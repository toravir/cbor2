from __future__ import division

import re
import math
import struct
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from functools import wraps
from datetime import datetime, date, time, tzinfo
from io import BytesIO
from sys import modules

from .compat import (
    iteritems, timezone, long, int2bytes, unicode, as_unicode, pack_float16,
    unpack_float16)
from .types import (
    CBOREncodeError, CBORTag, undefined, CBORSimpleValue, FrozenDict)


def shareable_encoder(func):
    """
    Wrap the given encoder function to gracefully handle cyclic data
    structures.

    If value sharing is enabled, this marks the given value shared in the
    datastream on the first call. If the value has already been passed to this
    method, a reference marker is instead written to the data stream and the
    wrapped function is not called.

    If value sharing is disabled, only infinite recursion protection is done.
    """
    @wraps(func)
    def wrapper(encoder, value):
        encoder.encode_shared(func, value)
    return wrapper


class CBOREncoder(object):
    """
    The CBOREncoder class implements a fully featured `CBOR`_ encoder with
    several extensions for handling shared references, big integers, rational
    numbers and so on. Typically the class is not used directly, but the
    :func:`dump` and :func:`dumps` functions are called to indirectly construct
    and use the class.

    When the class is constructed manually, the main entry points are
    :meth:`encode` and :meth:`encode_to_bytes`.

    :param bool datetime_as_timestamp:
        set to ``True`` to serialize datetimes as UNIX timestamps (this makes
        datetimes more concise on the wire, but loses the timezone information)
    :param datetime.tzinfo timezone:
        the default timezone to use for serializing naive datetimes; if this is
        not specified naive datetimes will throw a :exc:`ValueError` when
        encoding is attempted
    :param bool value_sharing:
        set to ``True`` to allow more efficient serializing of repeated values
        and, more importantly, cyclic data structures, at the cost of extra
        line overhead
    :param default:
        a callable that is called by the encoder with two arguments (the
        encoder instance and the value being encoded) when no suitable encoder
        has been found, and should use the methods on the encoder to encode any
        objects it wants to add to the data stream
    :param bool canonical:
        when True, use "canonical" CBOR representation; this typically involves
        sorting maps, sets, etc. into a pre-determined order ensuring that
        serializations are comparable without decoding

    .. _CBOR: https://cbor.io/
    """

    __slots__ = (
        'datetime_as_timestamp', '_timezone', '_default', 'value_sharing',
        'json_compatible', '_fp_write', '_shared_containers', '_encoders',
        '_canonical')

    def __init__(self, fp, datetime_as_timestamp=False, timezone=None,
                 value_sharing=False, default=None, canonical=False):
        self.fp = fp
        self.datetime_as_timestamp = datetime_as_timestamp
        self.timezone = timezone
        self.value_sharing = value_sharing
        self.default = default
        self._canonical = canonical
        self._shared_containers = {}  # indexes used for value sharing
        self._encoders = default_encoders.copy()
        if canonical:
            self._encoders.update(canonical_encoders)

    def _find_encoder(self, obj_type):
        for type_, enc in list(iteritems(self._encoders)):
            if type(type_) is tuple:
                try:
                    modname, typename = type_
                except (TypeError, ValueError):
                    raise CBOREncodeError(
                        "invalid deferred encoder type {!r} (must be a "
                        "2-tuple of module name and type name, e.g. "
                        "('collections', 'defaultdict'))".format(type_))
                imported_type = getattr(modules.get(modname), typename, None)
                if imported_type is not None:
                    del self._encoders[type_]
                    self._encoders[imported_type] = enc
                    type_ = imported_type
                else:  # pragma: nocover
                    continue

            if issubclass(obj_type, type_):
                self._encoders[obj_type] = enc
                return enc

        return None

    @property
    def fp(self):
        return self._fp_write.__self__

    @fp.setter
    def fp(self, value):
        try:
            if not callable(value.write):
                raise ValueError('fp.write is not callable')
        except AttributeError:
            raise ValueError('fp object has no write method')
        else:
            self._fp_write = value.write

    @property
    def timezone(self):
        return self._timezone

    @timezone.setter
    def timezone(self, value):
        if value is None or isinstance(value, tzinfo):
            self._timezone = value
        else:
            raise ValueError('timezone must be None or a tzinfo instance')

    @property
    def default(self):
        return self._default

    @default.setter
    def default(self, value):
        if value is None or callable(value):
            self._default = value
        else:
            raise ValueError('default must be None or a callable')

    @property
    def canonical(self):
        return self._canonical

    @contextmanager
    def disable_value_sharing(self):
        """
        Disable value sharing in the encoder for the duration of the context
        block.
        """
        old_value_sharing = self.value_sharing
        self.value_sharing = False
        yield
        self.value_sharing = old_value_sharing

    def write(self, data):
        """
        Write bytes to the data stream.

        :param bytes data:
            the bytes to write
        """
        self._fp_write(data)

    def encode(self, obj):
        """
        Encode the given object using CBOR.

        :param obj:
            the object to encode
        """
        obj_type = obj.__class__
        encoder = (
            self._encoders.get(obj_type) or
            self._find_encoder(obj_type) or
            self._default
        )
        if not encoder:
            raise CBOREncodeError('cannot serialize type %s' % obj_type.__name__)

        encoder(self, obj)

    def encode_to_bytes(self, obj):
        """
        Encode the given object to a byte buffer and return its value as bytes.

        This method was intended to be used from the ``default`` hook when an
        object needs to be encoded separately from the rest but while still
        taking advantage of the shared value registry.
        """
        with BytesIO() as fp:
            old_fp = self.fp
            self.fp = fp
            self.encode(obj)
            self.fp = old_fp
            return fp.getvalue()

    def encode_shared(self, encoder, value):
        value_id = id(value)
        try:
            index = self._shared_containers[id(value)][1]
        except KeyError:
            if self.value_sharing:
                # Mark the container as shareable
                self._shared_containers[value_id] = (
                    value, len(self._shared_containers)
                )
                self.encode_length(6, 0x1c)
                encoder(self, value)
            else:
                self._shared_containers[value_id] = (value, None)
                try:
                    encoder(self, value)
                finally:
                    del self._shared_containers[value_id]
        else:
            if self.value_sharing:
                # Generate a reference to the previous index instead of
                # encoding this again
                self.encode_length(6, 0x1d)
                self.encode_int(index)
            else:
                raise CBOREncodeError(
                    'cyclic data structure detected but value sharing is '
                    'disabled')

    def encode_length(self, major_tag, length):
        major_tag <<= 5
        if length < 24:
            self._fp_write(struct.pack('>B', major_tag | length))
        elif length < 256:
            self._fp_write(struct.pack('>BB', major_tag | 24, length))
        elif length < 65536:
            self._fp_write(struct.pack('>BH', major_tag | 25, length))
        elif length < 4294967296:
            self._fp_write(struct.pack('>BL', major_tag | 26, length))
        else:
            self._fp_write(struct.pack('>BQ', major_tag | 27, length))

    def encode_int(self, value):
        # Big integers (2 ** 64 and over)
        if value >= 18446744073709551616 or value < -18446744073709551616:
            if value >= 0:
                major_type = 0x02
            else:
                major_type = 0x03
                value = -value - 1

            payload = int2bytes(value)
            self.encode_semantic(CBORTag(major_type, payload))
        elif value >= 0:
            self.encode_length(0, value)
        else:
            self.encode_length(1, -(value + 1))

    def encode_bytestring(self, value):
        self.encode_length(2, len(value))
        self._fp_write(value)

    def encode_bytearray(self, value):
        self.encode_bytestring(bytes(value))

    def encode_string(self, value):
        encoded = value.encode('utf-8')
        self.encode_length(3, len(encoded))
        self._fp_write(encoded)

    @shareable_encoder
    def encode_array(self, value):
        self.encode_length(4, len(value))
        for item in value:
            self.encode(item)

    @shareable_encoder
    def encode_map(self, value):
        self.encode_length(5, len(value))
        for key, val in value.items():
            self.encode(key)
            self.encode(val)

    def encode_sortable_key(self, value):
        """
        Takes a key and calculates the length of its optimal byte
        representation, along with the representation itself. This is used as
        the sorting key in CBOR's canonical representations.
        """
        encoded = self.encode_to_bytes(value)
        return len(encoded), encoded

    @shareable_encoder
    def encode_canonical_map(self, value):
        "Reorder keys according to Canonical CBOR specification"
        keyed_keys = (
            (self.encode_sortable_key(key), key, value)
            for key, value in value.items()
        )
        self.encode_length(5, len(value))
        for sortkey, realkey, value in sorted(keyed_keys):
            self._fp_write(sortkey[1])
            self.encode(value)

    def encode_semantic(self, value):
        self.encode_length(6, value.tag)
        self.encode(value.value)

    #
    # Semantic decoders (major tag 6)
    #

    def encode_datetime(self, value):
        # Semantic tag 0
        if not value.tzinfo:
            if self._timezone:
                value = value.replace(tzinfo=self._timezone)
            else:
                raise CBOREncodeError(
                    'naive datetime {!r} encountered and no default timezone '
                    'has been set'.format(value))

        if self.datetime_as_timestamp:
            from calendar import timegm
            if not value.microsecond:
                timestamp = timegm(value.utctimetuple())
            else:
                timestamp = timegm(value.utctimetuple()) + value.microsecond / 1000000
            self.encode_semantic(CBORTag(1, timestamp))
        else:
            datestring = as_unicode(value.isoformat().replace('+00:00', 'Z'))
            self.encode_semantic(CBORTag(0, datestring))

    def encode_date(self, value):
        value = datetime.combine(value, time()).replace(tzinfo=timezone.utc)
        self.encode_datetime(value)

    def encode_decimal(self, value):
        # Semantic tag 4
        if value.is_nan():
            self._fp_write(b'\xf9\x7e\x00')
        elif value.is_infinite():
            self._fp_write(b'\xf9\x7c\x00' if value > 0 else b'\xf9\xfc\x00')
        else:
            dt = value.as_tuple()
            sig = 0
            for digit in dt.digits:
                sig = (sig * 10) + digit
            if dt.sign:
                sig = -sig
            with self.disable_value_sharing():
                self.encode_semantic(CBORTag(4, [dt.exponent, sig]))

    def encode_rational(self, value):
        # Semantic tag 30
        with self.disable_value_sharing():
            self.encode_semantic(CBORTag(30, [value.numerator, value.denominator]))

    def encode_regexp(self, value):
        # Semantic tag 35
        self.encode_semantic(CBORTag(35, as_unicode(value.pattern)))

    def encode_mime(self, value):
        # Semantic tag 36
        self.encode_semantic(CBORTag(36, as_unicode(value.as_string())))

    def encode_uuid(self, value):
        # Semantic tag 37
        self.encode_semantic(CBORTag(37, value.bytes))

    def encode_set(self, value):
        # Semantic tag 258
        self.encode_semantic(CBORTag(258, tuple(value)))

    def encode_canonical_set(self, value):
        # Semantic tag 258
        values = sorted(
            (self.encode_sortable_key(key), key)
            for key in value
        )
        self.encode_semantic(CBORTag(258, [key[1] for key in values]))

    def encode_ipaddress(self, value):
        # Semantic tag 260
        self.encode_semantic(CBORTag(260, value.packed))

    def encode_ipnetwork(self, value):
        # Semantic tag 261
        self.encode_semantic(
            CBORTag(261, {value.network_address.packed: value.prefixlen}))

    #
    # Special encoders (major tag 7)
    #

    def encode_simple_value(self, value):
        if value.value < 20:
            self._fp_write(struct.pack('>B', 0xe0 | value.value))
        else:
            self._fp_write(struct.pack('>BB', 0xf8, value.value))

    def encode_float(self, value):
        # Handle special values efficiently
        if math.isnan(value):
            self._fp_write(b'\xf9\x7e\x00')
        elif math.isinf(value):
            self._fp_write(b'\xf9\x7c\x00' if value > 0 else b'\xf9\xfc\x00')
        else:
            self._fp_write(struct.pack('>Bd', 0xfb, value))

    def encode_minimal_float(self, value):
        # Handle special values efficiently
        if math.isnan(value):
            self._fp_write(b'\xf9\x7e\x00')
        elif math.isinf(value):
            self._fp_write(b'\xf9\x7c\x00' if value > 0 else b'\xf9\xfc\x00')
        else:
            # Try each encoding in turn from longest to shortest
            encoded = struct.pack('>Bd', 0xfb, value)
            for format, tag in [('>Bf', 0xfa), ('>Be', 0xf9)]:
                try:
                    new_encoded = struct.pack(format, tag, value)
                    # Check if encoding as low-byte float loses precision
                    if struct.unpack(format, new_encoded)[1] == value:
                        encoded = new_encoded
                    else:
                        break
                except struct.error:
                    # Catch the case where the 'e' format is not supported
                    new_encoded = pack_float16(value)
                    if new_encoded and unpack_float16(new_encoded[1:]) == value:
                        encoded = new_encoded
                    else:
                        break
                except OverflowError:
                    break
            self._fp_write(encoded)

    def encode_boolean(self, value):
        self._fp_write(b'\xf5' if value else b'\xf4')

    def encode_none(self, value):
        self._fp_write(b'\xf6')

    def encode_undefined(self, value):
        self._fp_write(b'\xf7')


default_encoders = OrderedDict([
    (bytes,                         CBOREncoder.encode_bytestring),
    (bytearray,                     CBOREncoder.encode_bytearray),
    (unicode,                       CBOREncoder.encode_string),
    (int,                           CBOREncoder.encode_int),
    (long,                          CBOREncoder.encode_int),
    (float,                         CBOREncoder.encode_float),
    (('decimal', 'Decimal'),        CBOREncoder.encode_decimal),
    (bool,                          CBOREncoder.encode_boolean),
    (type(None),                    CBOREncoder.encode_none),
    (tuple,                         CBOREncoder.encode_array),
    (list,                          CBOREncoder.encode_array),
    (dict,                          CBOREncoder.encode_map),
    (defaultdict,                   CBOREncoder.encode_map),
    (OrderedDict,                   CBOREncoder.encode_map),
    (FrozenDict,                    CBOREncoder.encode_map),
    (type(undefined),               CBOREncoder.encode_undefined),
    (datetime,                      CBOREncoder.encode_datetime),
    (date,                          CBOREncoder.encode_date),
    (type(re.compile('')),          CBOREncoder.encode_regexp),
    (('fractions', 'Fraction'),     CBOREncoder.encode_rational),
    (('email.message', 'Message'),  CBOREncoder.encode_mime),
    (('uuid', 'UUID'),              CBOREncoder.encode_uuid),
    (('ipaddress', 'IPv4Address'),  CBOREncoder.encode_ipaddress),
    (('ipaddress', 'IPv6Address'),  CBOREncoder.encode_ipaddress),
    (('ipaddress', 'IPv4Network'),  CBOREncoder.encode_ipnetwork),
    (('ipaddress', 'IPv6Network'),  CBOREncoder.encode_ipnetwork),
    (CBORSimpleValue,               CBOREncoder.encode_simple_value),
    (CBORTag,                       CBOREncoder.encode_semantic),
    (set,                           CBOREncoder.encode_set),
    (frozenset,                     CBOREncoder.encode_set),
])


canonical_encoders = OrderedDict([
    (float,       CBOREncoder.encode_minimal_float),
    (dict,        CBOREncoder.encode_canonical_map),
    (defaultdict, CBOREncoder.encode_canonical_map),
    (OrderedDict, CBOREncoder.encode_canonical_map),
    (FrozenDict,  CBOREncoder.encode_canonical_map),
    (set,         CBOREncoder.encode_canonical_set),
    (frozenset,   CBOREncoder.encode_canonical_set),
])


def dumps(obj, **kwargs):
    """
    Serialize an object to a bytestring.

    :param obj: the object to serialize
    :param kwargs: keyword arguments passed to :class:`~.CBOREncoder`
    :return: the serialized output
    :rtype: bytes

    """
    with BytesIO() as fp:
        dump(obj, fp, **kwargs)
        return fp.getvalue()


def dump(obj, fp, **kwargs):
    """
    Serialize an object to a file.

    :param obj: the object to serialize
    :param fp: a file-like object
    :param kwargs: keyword arguments passed to :class:`~.CBOREncoder`

    """
    CBOREncoder(fp, **kwargs).encode(obj)
