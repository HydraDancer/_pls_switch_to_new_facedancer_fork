#
# This file is part of facedancer.
#
""" Functionality for defining USB interfaces. """

import struct

from typing       import Dict, Iterable
from dataclasses  import dataclass, field

from .magic       import instantiate_subordinates, AutoInstantiable
from .types       import USBDirection, USBStandardRequests

from .            import device
from .descriptor  import USBDescribable, USBDescriptor, USBClassDescriptor, USBDescriptorTypeNumber
from .request     import USBControlRequest, USBRequestHandler, get_request_handler_methods
from .request     import standard_request_handler, to_this_interface
from .endpoint    import USBEndpoint

from .logging     import log


@dataclass
class USBInterface(USBDescribable, AutoInstantiable, USBRequestHandler):
    """ Class representing a USBDevice interface.

    Fields:
        number :
            The interface's index. Zero indexed.
        class_number, subclass_number, protocol_number :
            The USB class adhered to on this interface; usually a USBDeviceClass constant.
        interface_string :
            A short, descriptive string used to identify the endpoint; or None if not provided.
    """
    DESCRIPTOR_TYPE_NUMBER = 0x4

    name                   : str = "generic USB interface"

    number                 : int = 0
    alternate              : int = 0

    class_number           : int = 0
    subclass_number        : int = 0
    protocol_number        : int = 0

    interface_string       : str = None

    descriptors            : Dict[int, bytes] = field(default_factory=dict)
    class_descriptor       : bytes = None

    endpoints              : Dict[int, USBEndpoint] = field(default_factory=dict)
    parent                 : USBDescribable = None


    @classmethod
    def from_binary_descriptor(cls, data):
        """
        Generates an interface object from a descriptor.
        """
        interface_number, alternate_setting, num_endpoints, interface_class, \
                interface_subclass, interface_protocol, interface_string_index \
                = struct.unpack_from("xxBBBBBBB", data)
        return cls(
            name=None,
            number=interface_number,
            alternate=alternate_setting,
            class_number=interface_class,
            subclass_number=interface_subclass,
            protocol_number=interface_protocol,
            interface_string=interface_string_index
        )


    def __post_init__(self):

        # Capture any descriptors/endpoints declared directly on the class.
        self.endpoints.update(instantiate_subordinates(self, USBEndpoint))
        self.descriptors.update(instantiate_subordinates(self, USBDescriptor))

        # If we weren't provided with a class descriptor, try to find one
        # provided on the class.
        if self.class_descriptor is None:
            class_descriptors = instantiate_subordinates(self, USBClassDescriptor)
            self.class_descriptor = list(class_descriptors.values())[0] if class_descriptors else None

        # Add in our general interface-descriptor handler.
        self.descriptors[USBDescriptorTypeNumber.INTERFACE] = self.get_descriptor

        # Populate our request handlers.
        self._request_handler_methods = get_request_handler_methods(self)


    #
    # User interface.
    #

    def get_device(self):
        """ Returns the device associated with the given descriptor. """
        return self.parent.get_device()


    def add_endpoint(self, endpoint: USBEndpoint):
        """ Adds the provided endpoint to the interface. """
        self.endpoints[endpoint.get_identifier()] = endpoint
        endpoint.parent = self


    def get_endpoint(self, endpoint_number: int, direction: USBDirection) -> USBEndpoint:
        """ Attempts to find a subordinate endpoint matching the given number/direction.

        Args:
            endpoint_number : The endpoint number to search for.
            direction       : The endpoint direction to be matched.

        Returns:
            The matching endpoint; or None if no matching endpoint existed.
        """
        address = USBEndpoint.address_for_number(endpoint_number, direction)
        return self.endpoints.get(address, None)


    def has_endpoint(self, endpoint_number: int, direction: USBDirection) -> USBEndpoint:
        """ Returns true iff we have matching subordinate endpoint.

        Args:
            endpoint_number : The endpoint number to search for.
            direction       : The endpoint direction to be matched.
        """
        return (self.get_endpoint(endpoint_number, direction) is not None)


    #
    # Event handlers.
    #

    def handle_data_received(self, endpoint: USBEndpoint, data: bytes):
        """ Handler for receipt of non-control request data.

        Typically, this method will delegate any data received to the
        appropriate configuration/interface/endpoint. If overridden, the
        overriding function will receive all data; and can delegate it by
        calling the `.handle_data_received` method on `self.configuration`.

        Args:
            endpoint_number : The endpoint number on which the data was received.
            data            : The raw bytes received on the relevant endpoint.
        """

        if self.has_endpoint(endpoint.number, endpoint.direction):
            endpoint.handle_data_received(data)
        else:
            self.get_device().handle_unexpected_data_received(endpoint.number, data)


    def handle_data_requested(self, endpoint: USBEndpoint):
        """ Handler called when the host requests data on a non-control endpoint.

        Typically, this method will delegate the request to the appropriate
        interface+endpoint. If overridden, the overriding function will receive
        all data.

        Args:
            endpoint_number : The endpoint number on which the host requested data.
        """

        if self.has_endpoint(endpoint.number, endpoint.direction):
            endpoint.handle_data_requested()
        else:
            self.get_device().handle_unexpected_data_requested(endpoint.number)


    def handle_buffer_empty(self, endpoint: USBEndpoint):
        """ Handler called when a given endpoint first has an empty buffer.

        Often, an empty buffer indicates an opportunity to queue data
        for sending ('prime an endpoint'), but doesn't necessarily mean
        that the host is planning on reading the data.

        This function is called only once per buffer.
        """

        if self.has_endpoint(endpoint.number, endpoint.direction):
            endpoint.handle_buffer_empty()


    #
    # Backend helpers.
    #

    def get_endpoints(self):
        """ Returns an iterable over all endpoints in this interface. """
        return self.endpoints.values()


    #
    # Internal interface.
    #

    @standard_request_handler(number=USBStandardRequests.GET_DESCRIPTOR)
    @to_this_interface
    def handle_get_descriptor_request(self, request):
        """ Handle GET_DESCRIPTOR requests; per USB2 [9.4.3] """
        log.debug("Handling GET_DESCRIPTOR on endpoint.")

        # This is the same as the USBDevice get descriptor request => avoid duplication.
        self.get_device().handle_generic_get_descriptor_request(self, request)


    # Table 9-12 of USB 2.0 spec (pdf page 296)
    def get_descriptor(self) -> bytes:
        """ Retrieves the given interface's interface descriptor, with subordinates. """

        # FIXME: use construct

        string_manager = self.get_device().strings

        d = bytearray([
                9,          # length of descriptor in bytes
                4,          # descriptor type 4 == interface
                self.number,
                self.alternate,
                len(self.endpoints),
                self.class_number,
                self.subclass_number,
                self.protocol_number,
                string_manager.get_index(self.interface_string)
        ])

        # If we have a class object, append its class descriptor...
        if self.class_descriptor:
            if callable(self.class_descriptor):
                d += self.class_descriptor()
            else:
                d += self.class_descriptor

        # ... append each endpoint's endpoint descriptor.
        for e in self.endpoints.values():
            d += e.get_descriptor()

        return d

    #
    # Alternate interface support.
    #

    # TODO: support these!

    @standard_request_handler(number=USBStandardRequests.SET_INTERFACE)
    @to_this_interface
    def handle_set_interface_request(self, request: USBControlRequest):
        """ Handle SET_INTERFACE requests; per USB2 [9.4.10] """
        logging.debug(f"f{self.name} received SET_INTERFACE request")
        # We don't support alternate interface settings; so ACK
        # alternate setting zero, and stall all others.
        if request.value == 0:
            request.acknowledge()
        else:
            request.stall()


    #
    # Automatic instantiation support.
    #

    def get_identifier(self) -> int:
        return self.number

    #
    # Request handler functions.
    #

    def _request_handlers(self) -> Iterable[callable]:
        return self._request_handler_methods

    def _get_subordinate_handlers(self) -> Iterable[callable]:
        return self.endpoints.values()
