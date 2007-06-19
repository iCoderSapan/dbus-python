# Copyright (C) 2003, 2004, 2005, 2006 Red Hat Inc. <http://www.redhat.com/>
# Copyright (C) 2003 David Zeuthen
# Copyright (C) 2004 Rob Taylor
# Copyright (C) 2005, 2006 Collabora Ltd. <http://www.collabora.co.uk/>
#
# Licensed under the Academic Free License version 2.1
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

__all__ = ('BusName', 'Object', 'method', 'signal')
__docformat__ = 'restructuredtext'

import sys
import logging
import operator
import traceback
try:
    import thread
except ImportError:
    import dummy_thread as thread

import _dbus_bindings
from dbus import SessionBus
from dbus.exceptions import DBusException, \
                            NameExistsException, \
                            UnknownMethodException
from dbus.decorators import method
from dbus.decorators import signal
from dbus.proxies import LOCAL_PATH


_logger = logging.getLogger('dbus.service')


class _VariantSignature(object):
    """A fake method signature which, when iterated, yields an endless stream
    of 'v' characters representing variants (handy with zip()).

    It has no string representation.
    """
    def __iter__(self):
        """Return self."""
        return self

    def next(self):
        """Return 'v' whenever called."""
        return 'v'

class BusName(object):
    """A base class for exporting your own Named Services across the Bus.

    When instantiated, objects of this class attempt to claim the given
    well-known name on the given bus for the current process. The name is
    released when the BusName object becomes unreferenced.

    If a well-known name is requested multiple times, multiple references
    to the same BusName object will be returned.

    Caveats
    -------
    - Assumes that named services are only ever requested using this class -
      if you request names from the bus directly, confusion may occur.
    - Does not handle queueing.
    """
    def __new__(cls, name, bus=None, allow_replacement=False , replace_existing=False, do_not_queue=False):
        """Constructor, which may either return an existing cached object
        or a new object.

        :Parameters:
            `name` : str
                The well-known name to be advertised
            `bus` : dbus.Bus
                A Bus on which this service will be advertised; if None
                (default) the session bus will be used
            `allow_replacement` : bool
                If True, other processes trying to claim the same well-known
                name will take precedence over this one.
            `replace_existing` : bool
                If True, this process can take over the well-known name
                from other processes already holding it.
            `do_not_queue` : bool
                If True, this service will not be placed in the queue of
                services waiting for the requested name if another service
                already holds it.
        """
        _dbus_bindings.validate_bus_name(name, allow_well_known=True,
                                         allow_unique=False)

        # get default bus
        if bus == None:
            bus = SessionBus()

        # see if this name is already defined, return it if so
        # FIXME: accessing internals of Bus
        if name in bus._bus_names:
            return bus._bus_names[name]

        # otherwise register the name
        name_flags = (
            (allow_replacement and _dbus_bindings.NAME_FLAG_ALLOW_REPLACEMENT or 0) |
            (replace_existing and _dbus_bindings.NAME_FLAG_REPLACE_EXISTING or 0) |
            (do_not_queue and _dbus_bindings.NAME_FLAG_DO_NOT_QUEUE or 0))

        retval = bus.request_name(name, name_flags)

        # TODO: more intelligent tracking of bus name states?
        if retval == _dbus_bindings.REQUEST_NAME_REPLY_PRIMARY_OWNER:
            pass
        elif retval == _dbus_bindings.REQUEST_NAME_REPLY_IN_QUEUE:
            # queueing can happen by default, maybe we should
            # track this better or let the user know if they're
            # queued or not?
            pass
        elif retval == _dbus_bindings.REQUEST_NAME_REPLY_EXISTS:
            raise NameExistsException(name)
        elif retval == _dbus_bindings.REQUEST_NAME_REPLY_ALREADY_OWNER:
            # if this is a shared bus which is being used by someone
            # else in this process, this can happen legitimately
            pass
        else:
            raise RuntimeError('requesting bus name %s returned unexpected value %s' % (name, retval))

        # and create the object
        bus_name = object.__new__(cls)
        bus_name._bus = bus
        bus_name._name = name

        # cache instance (weak ref only)
        # FIXME: accessing Bus internals again
        bus._bus_names[name] = bus_name

        return bus_name

    # do nothing because this is called whether or not the bus name
    # object was retrieved from the cache or created new
    def __init__(self, *args, **keywords):
        pass

    # we can delete the low-level name here because these objects
    # are guaranteed to exist only once for each bus name
    def __del__(self):
        self._bus.release_name(self._name)
        pass

    def get_bus(self):
        """Get the Bus this Service is on"""
        return self._bus

    def get_name(self):
        """Get the name of this service"""
        return self._name

    def __repr__(self):
        return '<dbus.service.BusName %s on %r at %#x>' % (self._name, self._bus, id(self))
    __str__ = __repr__


def _method_lookup(self, method_name, dbus_interface):
    """Walks the Python MRO of the given class to find the method to invoke.

    Returns two methods, the one to call, and the one it inherits from which
    defines its D-Bus interface name, signature, and attributes.
    """
    parent_method = None
    candidate_class = None
    successful = False

    # split up the cases when we do and don't have an interface because the
    # latter is much simpler
    if dbus_interface:
        # search through the class hierarchy in python MRO order
        for cls in self.__class__.__mro__:
            # if we haven't got a candidate class yet, and we find a class with a
            # suitably named member, save this as a candidate class
            if (not candidate_class and method_name in cls.__dict__):
                if ("_dbus_is_method" in cls.__dict__[method_name].__dict__
                    and "_dbus_interface" in cls.__dict__[method_name].__dict__):
                    # however if it is annotated for a different interface
                    # than we are looking for, it cannot be a candidate
                    if cls.__dict__[method_name]._dbus_interface == dbus_interface:
                        candidate_class = cls
                        parent_method = cls.__dict__[method_name]
                        successful = True
                        break
                    else:
                        pass
                else:
                    candidate_class = cls

            # if we have a candidate class, carry on checking this and all
            # superclasses for a method annoated as a dbus method
            # on the correct interface
            if (candidate_class and method_name in cls.__dict__
                and "_dbus_is_method" in cls.__dict__[method_name].__dict__
                and "_dbus_interface" in cls.__dict__[method_name].__dict__
                and cls.__dict__[method_name]._dbus_interface == dbus_interface):
                # the candidate class has a dbus method on the correct interface,
                # or overrides a method that is, success!
                parent_method = cls.__dict__[method_name]
                successful = True
                break

    else:
        # simpler version of above
        for cls in self.__class__.__mro__:
            if (not candidate_class and method_name in cls.__dict__):
                candidate_class = cls

            if (candidate_class and method_name in cls.__dict__
                and "_dbus_is_method" in cls.__dict__[method_name].__dict__):
                parent_method = cls.__dict__[method_name]
                successful = True
                break

    if successful:
        return (candidate_class.__dict__[method_name], parent_method)
    else:
        if dbus_interface:
            raise UnknownMethodException('%s is not a valid method of interface %s' % (method_name, dbus_interface))
        else:
            raise UnknownMethodException('%s is not a valid method' % method_name)


def _method_reply_return(connection, message, method_name, signature, *retval):
    reply = _dbus_bindings.MethodReturnMessage(message)
    try:
        reply.append(signature=signature, *retval)
    except Exception, e:
        logging.basicConfig()
        if signature is None:
            try:
                signature = reply.guess_signature(retval) + ' (guessed)'
            except Exception, e:
                _logger.error('Unable to guess signature for arguments %r: '
                              '%s: %s', retval, e.__class__, e)
                raise
        _logger.error('Unable to append %r to message with signature %s: '
                      '%s: %s', retval, signature, e.__class__, e)
        raise

    connection.send_message(reply)


def _method_reply_error(connection, message, exception):
    name = getattr(exception, '_dbus_error_name', None)

    if name is not None:
        pass
    elif getattr(exception, '__module__', '') in ('', '__main__'):
        name = 'org.freedesktop.DBus.Python.%s' % exception.__class__.__name__
    else:
        name = 'org.freedesktop.DBus.Python.%s.%s' % (exception.__module__, exception.__class__.__name__)

    contents = traceback.format_exc()
    reply = _dbus_bindings.ErrorMessage(message, name, contents)

    connection.send_message(reply)


class InterfaceType(type):
    def __init__(cls, name, bases, dct):
        # these attributes are shared between all instances of the Interface
        # object, so this has to be a dictionary that maps class names to
        # the per-class introspection/interface data
        class_table = getattr(cls, '_dbus_class_table', {})
        cls._dbus_class_table = class_table
        interface_table = class_table[cls.__module__ + '.' + name] = {}

        # merge all the name -> method tables for all the interfaces
        # implemented by our base classes into our own
        for b in bases:
            base_name = b.__module__ + '.' + b.__name__
            if getattr(b, '_dbus_class_table', False):
                for (interface, method_table) in class_table[base_name].iteritems():
                    our_method_table = interface_table.setdefault(interface, {})
                    our_method_table.update(method_table)

        # add in all the name -> method entries for our own methods/signals
        for func in dct.values():
            if getattr(func, '_dbus_interface', False):
                method_table = interface_table.setdefault(func._dbus_interface, {})
                method_table[func.__name__] = func

        super(InterfaceType, cls).__init__(name, bases, dct)

    # methods are different to signals, so we have two functions... :)
    def _reflect_on_method(cls, func):
        args = func._dbus_args

        if func._dbus_in_signature:
            # convert signature into a tuple so length refers to number of
            # types, not number of characters. the length is checked by
            # the decorator to make sure it matches the length of args.
            in_sig = tuple(_dbus_bindings.Signature(func._dbus_in_signature))
        else:
            # magic iterator which returns as many v's as we need
            in_sig = _VariantSignature()

        if func._dbus_out_signature:
            out_sig = _dbus_bindings.Signature(func._dbus_out_signature)
        else:
            # its tempting to default to _dbus_bindings.Signature('v'), but
            # for methods that return nothing, providing incorrect
            # introspection data is worse than providing none at all
            out_sig = []

        reflection_data = '    <method name="%s">\n' % (func.__name__)
        for pair in zip(in_sig, args):
            reflection_data += '      <arg direction="in"  type="%s" name="%s" />\n' % pair
        for type in out_sig:
            reflection_data += '      <arg direction="out" type="%s" />\n' % type
        reflection_data += '    </method>\n'

        return reflection_data

    def _reflect_on_signal(cls, func):
        args = func._dbus_args

        if func._dbus_signature:
            # convert signature into a tuple so length refers to number of
            # types, not number of characters
            sig = tuple(_dbus_bindings.Signature(func._dbus_signature))
        else:
            # magic iterator which returns as many v's as we need
            sig = _VariantSignature()

        reflection_data = '    <signal name="%s">\n' % (func.__name__)
        for pair in zip(sig, args):
            reflection_data = reflection_data + '      <arg type="%s" name="%s" />\n' % pair
        reflection_data = reflection_data + '    </signal>\n'

        return reflection_data

class Interface(object):
    __metaclass__ = InterfaceType

#: A unique object used as the value of Object._object_path and
#: Object._connection if it's actually in more than one place
_MANY = object()

class Object(Interface):
    r"""A base class for exporting your own Objects across the Bus.

    Just inherit from Object and mark exported methods with the
    @\ `dbus.service.method` or @\ `dbus.service.signal` decorator.

    Example::

        class Example(dbus.service.object):
            def __init__(self, object_path):
                dbus.service.Object.__init__(self, dbus.SessionBus(), path)
                self._last_input = None

            @dbus.service.method(interface='com.example.Sample',
                                 in_signature='v', out_signature='s')
            def StringifyVariant(self, var):
                self.LastInputChanged(var)      # emits the signal
                return str(var)

            @dbus.service.signal(interface='com.example.Sample',
                                 signature='v')
            def LastInputChanged(self, var):
                # run just before the signal is actually emitted
                # just put "pass" if nothing should happen
                self._last_input = var

            @dbus.service.method(interface='com.example.Sample',
                                 in_signature='', out_signature='v')
            def GetLastInput(self):
                return self._last_input
    """

    #: If True, this object can be made available at more than one object path.
    #: If True but `SUPPORTS_MULTIPLE_CONNECTIONS` is False, the object may
    #: handle more than one object path, but they must all be on the same
    #: connection.
    SUPPORTS_MULTIPLE_OBJECT_PATHS = False

    #: If True, this object can be made available on more than one connection.
    #: If True but `SUPPORTS_MULTIPLE_OBJECT_PATHS` is False, the object must
    #: have the same object path on all its connections.
    SUPPORTS_MULTIPLE_CONNECTIONS = False

    def __init__(self, conn=None, object_path=None, bus_name=None):
        """Constructor. Either conn or bus_name is required; object_path
        is also required.

        :Parameters:
            `conn` : dbus.connection.Connection or None
                The connection on which to export this object.

                If None, use the Bus associated with the given ``bus_name``.
                If there is no ``bus_name`` either, the object is not
                initially available on any Connection.

                For backwards compatibility, if an instance of
                dbus.service.BusName is passed as the first parameter,
                this is equivalent to passing its associated Bus as
                ``conn``, and passing the BusName itself as ``bus_name``.

            `object_path` : str or None
                A D-Bus object path at which to make this Object available
                immediately. If this is not None, a `conn` or `bus_name` must
                also be provided.

            `bus_name` : dbus.service.BusName or None
                Represents a well-known name claimed by this process. A
                reference to the BusName object will be held by this
                Object, preventing the name from being released during this
                Object's lifetime (unless it's released manually).
        """
        if object_path is not None:
            _dbus_bindings.validate_object_path(object_path)

        if isinstance(conn, BusName):
            # someone's using the old API; don't gratuitously break them
            bus_name = conn
            conn = bus_name.get_bus()
        elif conn is None:
            if bus_name is not None:
                # someone's using the old API but naming arguments, probably
                conn = bus_name.get_bus()

        #: Either an object path, None or _MANY
        self._object_path = None
        #: Either a dbus.connection.Connection, None or _MANY
        self._connection = None
        #: A list of tuples (Connection, object path, False) where the False
        #: is for future expansion (to support fallback paths)
        self._locations = []
        #: Lock protecting `_locations`, `_connection` and `_object_path`
        self._locations_lock = thread.allocate_lock()

        #: True if this is a fallback object handling a whole subtree.
        self._fallback = False

        self._name = bus_name

        if conn is None and object_path is not None:
            raise TypeError('If object_path is given, either conn or bus_name '
                            'is required')
        if conn is not None and object_path is not None:
            self.add_to_connection(conn, object_path)

    @property
    def __dbus_object_path__(self):
        """The object-path at which this object is available.
        Access raises AttributeError if there is no object path, or more than
        one object path.

        Changed in 0.82.0: AttributeError can be raised.
        """
        if self._object_path is _MANY:
            raise AttributeError('Object %r has more than one object path: '
                                 'use Object.locations instead' % self)
        elif self._object_path is None:
            raise AttributeError('Object %r has no object path yet' % self)
        else:
            return self._object_path

    @property
    def connection(self):
        """The Connection on which this object is available.
        Access raises AttributeError if there is no Connection, or more than
        one Connection.

        Changed in 0.82.0: AttributeError can be raised.
        """
        if self._connection is _MANY:
            raise AttributeError('Object %r is on more than one Connection: '
                                 'use Object.locations instead' % self)
        elif self._connection is None:
            raise AttributeError('Object %r has no Connection yet' % self)
        else:
            return self._connection

    @property
    def locations(self):
        """An iterable over tuples representing locations at which this
        object is available.

        Each tuple has at least two items, but may have more in future
        versions of dbus-python, so do not rely on their exact length.
        The first two items are the dbus.connection.Connection and the object
        path.

        :Since: 0.82.0
        """
        return iter(self._locations)

    def add_to_connection(self, connection, path):
        """Make this object accessible via the given D-Bus connection and
        object path.

        :Parameters:
            `connection` : dbus.connection.Connection
                Export the object on this connection. If the class attribute
                SUPPORTS_MULTIPLE_CONNECTIONS is False (default), this object
                can only be made available on one connection; if the class
                attribute is set True by a subclass, the object can be made
                available on more than one connection.

            `path` : dbus.ObjectPath or other str
                Place the object at this object path. If the class attribute
                SUPPORTS_MULTIPLE_OBJECT_PATHS is False (default), this object
                can only be made available at one object path; if the class
                attribute is set True by a subclass, the object can be made
                available with more than one object path.

        :Raises ValueError: if the object's class attributes do not allow the
            object to be exported in the desired way.
        :Since: 0.82.0
        """
        if path == LOCAL_PATH:
            raise ValueError('Objects may not be exported on the reserved '
                             'path %s' % LOCAL_PATH)

        self._locations_lock.acquire()
        try:
            if (self._connection is not None and
                self._connection is not connection and
                not self.SUPPORTS_MULTIPLE_CONNECTIONS):
                raise ValueError('%r is already exported on '
                                 'connection %r' % (self, self._connection))

            if (self._object_path is not None and
                not self.SUPPORTS_MULTIPLE_OBJECT_PATHS and
                self._object_path != path):
                raise ValueError('%r is already exported at object '
                                 'path %s' % (self, self._object_path))

            connection._register_object_path(path, self._message_cb,
                                             self._unregister_cb,
                                             self._fallback)

            if self._connection is None:
                self._connection = connection
            elif self._connection is not connection:
                self._connection = _MANY

            if self._object_path is None:
                self._object_path = path
            elif self._object_path != path:
                self._object_path = _MANY

            self._locations.append((connection, path, self._fallback))
        finally:
            self._locations_lock.release()

    def remove_from_connection(self, connection=None, path=None):
        """Make this object inaccessible via the given D-Bus connection
        and object path. If no connection or path is specified,
        the object ceases to be accessible via any connection or path.

        It's not currently possible to export an object on more than one
        connection or with more than one object-path, but this will be
        supported in future.

        :Parameters:
            `connection` : dbus.connection.Connection or None
                Only remove the object from this Connection. If None,
                remove from all Connections on which it's exported.
            `path` : dbus.ObjectPath or other str, or None
                Only remove the object from this object path. If None,
                remove from all object paths.
        :Raises LookupError:
            if the object was not exported on the requested connection
            or path, or (if both are None) was not exported at all.
        :Since: 0.81.1
        """
        self._locations_lock.acquire()
        try:
            if self._object_path is None or self._connection is None:
                raise LookupError('%r is not exported' % self)

            if connection is not None or path is not None:
                dropped = []
                for location in self._locations:
                    if ((connection is None or location[0] is connection) and
                        (path is None or location[1] == path)):
                        dropped.append(location)
            else:
                dropped = self._locations
                self._locations = []

            if not dropped:
                raise LookupError('%r is not exported at a location matching '
                                  '(%r,%r)' % (self, connection, path))

            for location in dropped:
                try:
                    location[0]._unregister_object_path(location[1])
                except LookupError:
                    pass
                if self._locations:
                    try:
                        self._locations.remove(location)
                    except ValueError:
                        pass
        finally:
            self._locations_lock.release()

    def _unregister_cb(self, connection):
        # there's not really enough information to do anything useful here
        _logger.info('Unregistering exported object %r from some path '
                     'on %r', self, connection)

    def _message_cb(self, connection, message):
        try:
            # lookup candidate method and parent method
            method_name = message.get_member()
            interface_name = message.get_interface()
            (candidate_method, parent_method) = _method_lookup(self, method_name, interface_name)

            # set up method call parameters
            args = message.get_args_list(**parent_method._dbus_get_args_options)
            keywords = {}

            if parent_method._dbus_out_signature is not None:
                signature = _dbus_bindings.Signature(parent_method._dbus_out_signature)
            else:
                signature = None

            # set up async callback functions
            if parent_method._dbus_async_callbacks:
                (return_callback, error_callback) = parent_method._dbus_async_callbacks
                keywords[return_callback] = lambda *retval: _method_reply_return(connection, message, method_name, signature, *retval)
                keywords[error_callback] = lambda exception: _method_reply_error(connection, message, exception)

            # include the sender etc. if desired
            if parent_method._dbus_sender_keyword:
                keywords[parent_method._dbus_sender_keyword] = message.get_sender()
            if parent_method._dbus_path_keyword:
                keywords[parent_method._dbus_path_keyword] = message.get_path()
            if parent_method._dbus_destination_keyword:
                keywords[parent_method._dbus_destination_keyword] = message.get_destination()
            if parent_method._dbus_message_keyword:
                keywords[parent_method._dbus_message_keyword] = message
            if parent_method._dbus_connection_keyword:
                keywords[parent_method._dbus_connection_keyword] = connection

            # call method
            retval = candidate_method(self, *args, **keywords)

            # we're done - the method has got callback functions to reply with
            if parent_method._dbus_async_callbacks:
                return

            # otherwise we send the return values in a reply. if we have a
            # signature, use it to turn the return value into a tuple as
            # appropriate
            if signature is not None:
                signature_tuple = tuple(signature)
                # if we have zero or one return values we want make a tuple
                # for the _method_reply_return function, otherwise we need
                # to check we're passing it a sequence
                if len(signature_tuple) == 0:
                    if retval == None:
                        retval = ()
                    else:
                        raise TypeError('%s has an empty output signature but did not return None' %
                            method_name)
                elif len(signature_tuple) == 1:
                    retval = (retval,)
                else:
                    if operator.isSequenceType(retval):
                        # multi-value signature, multi-value return... proceed unchanged
                        pass
                    else:
                        raise TypeError('%s has multiple output values in signature %s but did not return a sequence' %
                            (method_name, signature))

            # no signature, so just turn the return into a tuple and send it as normal
            else:
                if retval is None:
                    retval = ()
                elif (isinstance(retval, tuple)
                      and not isinstance(retval, _dbus_bindings.Struct)):
                # If the return is a tuple that is not a Struct, we use it
                # as-is on the assumption that there are multiple return
                # values - this is the usual Python idiom. (fd.o #10174)
                    pass
                else:
                    retval = (retval,)

            _method_reply_return(connection, message, method_name, signature, *retval)
        except Exception, exception:
            # send error reply
            _method_reply_error(connection, message, exception)

    @method('org.freedesktop.DBus.Introspectable', in_signature='', out_signature='s')
    def Introspect(self):
        """Return a string of XML encoding this object's supported interfaces,
        methods and signals.
        """
        reflection_data = '<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN" "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">\n'
        reflection_data += '<node name="%s">\n' % (self._object_path)

        interfaces = self._dbus_class_table[self.__class__.__module__ + '.' + self.__class__.__name__]
        for (name, funcs) in interfaces.iteritems():
            reflection_data += '  <interface name="%s">\n' % (name)

            for func in funcs.values():
                if getattr(func, '_dbus_is_method', False):
                    reflection_data += self.__class__._reflect_on_method(func)
                elif getattr(func, '_dbus_is_signal', False):
                    reflection_data += self.__class__._reflect_on_signal(func)

            reflection_data += '  </interface>\n'

        for name in self._connection.list_exported_child_objects(
                self._object_path):
            reflection_data += '  <node name="%s"/>\n' % name

        reflection_data += '</node>\n'

        return reflection_data

    def __repr__(self):
        where = ''
        if (self._object_path is not _MANY
            and self._object_path is not None):
            where = ' at %s' % self._object_path
        return '<%s.%s%s at %#x>' % (self.__class__.__module__,
                                   self.__class__.__name__, where,
                                   id(self))
    __str__ = __repr__

class FallbackObject(Object):
    """An object that implements an entire subtree of the object-path
    tree.

    :Since: 0.82.0
    """

    SUPPORTS_MULTIPLE_OBJECT_PATHS = True

    def __init__(self, conn=None, object_path=None):
        """Constructor.

        Note that the superclass' ``bus_name`` __init__ argument is not
        supported here.

        :Parameters:
            `conn` : dbus.connection.Connection or None
                The connection on which to export this object. If this is not
                None, an `object_path` must also be provided.

                If None, the object is not initially available on any
                Connection.

            `object_path` : str or None
                A D-Bus object path at which to make this Object available
                immediately. If this is not None, a `conn` must also be
                provided.

                This object will implements all object-paths in the subtree
                starting at this object-path, except where a more specific
                object has been added.
        """
        super(FallbackObject, self).__init__()
        self._fallback = True

        if conn is None:
            if object_path is not None:
                raise TypeError('If object_path is given, conn is required')
        elif object_path is None:
            raise TypeError('If conn is given, object_path is required')
        else:
            self.add_to_connection(conn, object_path)
