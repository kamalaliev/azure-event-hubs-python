# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

"""
The module provides a client to connect to Azure Event Hubs. All service specifics
should be implemented in this module.

"""

import logging
import datetime
import sys
import threading
import uuid
import time
import asyncio
try:
    from urllib import urlparse
    from urllib import unquote_plus
except Exception:
    from urllib.parse import unquote_plus
    from urllib.parse import urlparse

import uamqp
from uamqp import Connection
from uamqp import SendClient, ReceiveClient
from uamqp import Message, BatchMessage
from uamqp import Source, Target
from uamqp import authentication
from uamqp import constants, types


__version__ = "0.2.0a1"

log = logging.getLogger(__name__)


def _parse_conn_str(conn_str):
    endpoint = None
    shared_access_key_name = None
    shared_access_key = None
    entity_path = None
    for element in conn_str.split(';'):
        key, _, value = element.partition('=')
        if key.lower() == 'endpoint':
            endpoint = value.rstrip('/')
        elif key.lower() == 'sharedaccesskeyname':
            shared_access_key_name = value
        elif key.lower() == 'sharedaccesskey':
            shared_access_key = value
        elif key.lower() == 'entitypath':
            entity_path = value
    if not all([endpoint, shared_access_key_name, shared_access_key]):
        raise ValueError("Invalid connection string")
    return endpoint, shared_access_key_name, shared_access_key, entity_path


def _build_uri(address, entity):
    parsed = urlparse(address)
    if parsed.path:
        print(parsed.path)
        return address
    if not entity:
        raise ValueError("No EventHub specified")
    address += "/" + str(entity)
    return address


class EventHubClient(object):
    """
    The EventHubClient class defines a high level interface for sending
    events to and receiving events from the Azure Event Hubs service.
    """

    def __init__(self, address, username=None, password=None, debug=False):
        """
        Constructs a new EventHubClient with the given address URL.

        :param address: The full URI string of the Event Hub. This can optionally
         include URL-encoded access name and key.
        :type address: str
        :param username: The name of the shared access policy. This must be supplied
         if not encoded into the address.
        :type username: str
        :param password: The shared access key. This must be supplied if not encoded
         into the address.
        :type password: str
        :param debug: Whether to output network trace logs to the logger. Default
         is `False`.
        :type debug: bool
        """
        self.container_id = "eventhub.pysdk-" + str(uuid.uuid4())[:8]
        self.address = urlparse(address)
        url_username = unquote_plus(self.address.username) if self.address.username else None
        username = username or url_username
        url_password = unquote_plus(self.address.password) if self.address.password else None
        password = password or url_password
        if not username or not password:
            raise ValueError("Missing username and/or password.")
        auth_uri = "sb://{}{}".format(self.address.hostname, self.address.path)
        self.auth = self._create_auth(auth_uri, username, password)
        self.connection = None
        self.debug = debug

        self.clients = []
        self.stopped = False
        log.info("{}: Created the Event Hub client".format(self.container_id))

    @classmethod
    def from_connection_string(cls, conn_str, eventhub=None, **kwargs):
        """
        Create an EventHubClient from a connection string.
        :param conn_str: The connection string.
        :type conn_str: str
        :param eventhub: The name of the EventHub, if the EntityName is
         not included in the connection string.
        """
        address, policy, key, entity = _parse_conn_str(conn_str)
        entity = entity or eventhub
        address = _build_uri(address, entity)
        return cls(address, username=policy, password=key, **kwargs)

    def _create_auth(self, auth_uri, username, password):
        """
        Create an ~uamqp.authentication.SASTokenAuth instance to authenticate
        the session.

        :param auth_uri: The URI to authenticate against.
        :type auth_uri: str
        :param username: The name of the shared access policy.
        :type username: str
        :param password: The shared access key.
        :type password: str
        """
        return authentication.SASTokenAuth.from_shared_access_key(auth_uri, username, password)

    def _create_properties(self):
        """
        Format the properties with which to instantiate the connection.
        This acts like a user agent over HTTP.

        :returns: dict
        """
        properties = {}
        properties["product"] = "eventhub.python"
        properties["version"] = __version__
        properties["framework"] = "Python {}.{}.{}".format(*sys.version_info[0:3])
        properties["platform"] = sys.platform
        return properties

    def _create_connection(self):
        """
        Create a new ~uamqp.Connection instance that will be shared between all
        Sender/Receiver clients.
        """
        if not self.connection:
            log.info("{}: Creating connection with address={}".format(self.container_id, self.address.geturl()))
            self.connection = Connection(
                self.address.hostname,
                self.auth,
                container_id=self.container_id,
                properties=self._create_properties(),
                debug=self.debug)

    def _close_connection(self):
        """
        Close and destroy the connection.
        """
        if self.connection:
            self.connection.destroy()
            self.connection = None

    def _close_clients(self):
        """
        Close all open Sender/Receiver clients.
        """
        for client in self.clients:
            client.close()

    def run(self):
        """
        Run the EventHubClient in blocking mode.
        Opens the connection and starts running all Sender/Receiver clients.

        :returns: ~azure.eventhub.EventHubClient
        """
        log.info("{}: Starting {} clients".format(self.container_id, len(self.clients)))
        self._create_connection()
        for client in self.clients:
            client.open(connection=self.connection)
        return self

    def stop(self):
        """
        Stop the EventHubClient and all its Sender/Receiver clients.
        """
        log.info("{}: Stopping {} clients".format(self.container_id, len(self.clients)))
        self.stopped = True
        self._close_clients()
        self._close_connection()

    def get_eventhub_info(self):
        """
        Get details on the specified EventHub.
        :returns: dict
        """
        eh_name = self.address.path.lstrip('/')
        target = "amqps://{}/{}".format(self.address.hostname, eh_name)
        with uamqp.AMQPClient(target, auth=self.auth, debug=self.debug) as mgmt_client:
            mgmt_msg = Message(application_properties={'name': eh_name})
            response = mgmt_client.mgmt_request(
                mgmt_msg,
                constants.READ_OPERATION,
                op_type=b'com.microsoft:eventhub',
                status_code_field=b'status-code',
                description_fields=b'status-description')
            return response.get_data()

    def add_receiver(self, consumer_group, partition, offset=None, prefetch=300):
        """
        Add a receiver to the client for a particular consumer group and partition.
        :param consumer_group: The name of the consumer group.
        :type consumer_group: str
        :param partition: The ID of the partition.
        :type partition: str
        :param offset: The offset from which to start receiving.
        :type offset: ~azure.eventhub.Offset
        :param prefetch: The message prefetch count of the receiver. Default is 300.
        :type prefetch: int
        :returns: ~azure.eventhub.Receiver
        """
        source_url = "amqps://{}{}/ConsumerGroups/{}/Partitions/{}".format(
            self.address.hostname, self.address.path, consumer_group, partition)
        source = Source(source_url)
        if offset is not None:
            source.set_filter(offset.selector())
        handler = Receiver(self, source, prefetch=prefetch)
        self.clients.append(handler._handler)
        return handler

    def add_epoch_receiver(self, consumer_group, partition, epoch, prefetch=300):
        """
        Add a receiver to the client with an epoch value. Only a single epoch receiver
        can connect to a partition at any given time - additional epoch receivers must have
        a higher epoch value or they will be rejected. If a 2nd epoch receiver has
        connected, the first will be closed.
        :param consumer_group: The name of the consumer group.
        :type consumer_group: str
        :param partition: The ID of the partition.
        :type partition: str
        :param epoch: The epoch value for the receiver.
        :type epoch: int
        :param prefetch: The message prefetch count of the receiver. Default is 300.
        :type prefetch: int
        :returns: ~azure.eventhub.Receiver
        """
        source_url = "amqps://{}{}/ConsumerGroups/{}/Partitions/{}".format(
            self.address.hostname, self.address.path, consumer_group, partition)
        handler = Receiver(self, source_url, prefetch=prefetch, epoch=epoch)
        self.clients.append(handler._handler)
        return handler

    def add_sender(self, partition=None):
        """
        Add a sender to the client to send ~azure.eventhub.EventData object
        to an EventHub.
        :param partition: Optionally specify a particular partition to send to.
         If omitted, the events will be distributed to available partitions via
         round-robin
        :type parition: str
        :returns: ~azure.eventhub.Sender
        """
        target = "amqps://{}{}".format(self.address.hostname, self.address.path)
        if partition:
            target += "/Partitions/" + partition
        handler = Sender(self, target)
        self.clients.append(handler._handler)
        return handler


class Sender:
    """
    Implements a Sender.
    """
    TIMEOUT = 60.0

    def __init__(self, client, target):
        """
        Instantiate an EventHub event Sender client.
        :param client: The parent EventHubClient.
        :type client: ~azure.eventhub.EventHubClient.
        :param target: The URI of the EventHub to send to.
        :type target: str
        """
        self._handler = SendClient(
            target,
            auth=client.auth,
            debug=client.debug,
            msg_timeout=Sender.TIMEOUT)
        self._outcome = None
        self._condition = None

    def send(self, event_data):
        """
        Sends an event data and blocks until acknowledgement is
        received or operation times out.
        :param event_data: The event to be sent.
        :type event_data: ~azure.eventhub.EventData
        :raises: ~azure.eventhub.EventHubError if the message fails to
         send.
        """
        event_data.message.on_send_complete = self._on_outcome
        self._handler.send_message(event_data.message)
        if self._outcome != constants.MessageSendResult.Ok:
            raise Sender._error(self._outcome, self._condition)

    def transfer(self, event_data, callback=None):
        """
        Transfers an event data and notifies the callback when the operation is done.
        :param event_data: The event to be sent.
        :type event_data: ~azure.eventhub.EventData
        :param callback: Callback to be run once the message has been send.
         This must be a function that accepts two arguments.
        :type callback: func[~uamqp.constants.MessageSendResult, ~azure.eventhub.EventHubError]
        """
        if callback:
            event_data.message.on_send_complete = lambda o, c: callback(o, Sender._error(o, c))
        self._handler.queue_message(event_data.message)

    def wait(self):
        """
        Wait until all transferred events have been sent.
        """
        self._handler.wait()

    def _on_outcome(self, outcome, condition):
        """
        Called when the outcome is received for a delivery.
        :param outcome: The outcome of the message delivery - success or failure.
        :type outcome: ~uamqp.constants.MessageSendResult
        """
        self._outcome = outcome
        self._condition = condition

    @staticmethod
    def _error(outcome, condition):
        return None if outcome == constants.MessageSendResult.Ok else EventHubError(outcome, condition)


class Receiver:
    """
    Implements a Receiver.
    """
    timeout = 0
    _epoch = b'com.microsoft:epoch'

    def __init__(self, client, source, prefetch=300, epoch=None):
        """
        Instantiate a receiver.
        :param client: The parent EventHubClient.
        :type client: ~azure.eventhub.EventHubClient
        :param source: The source EventHub from which to receive events.
        :type source: ~uamqp.Source
        :param prefetch: The number of events to prefetch from the service
         for processing. Default is 300.
        :type prefetch: int
        :param epoch: An optional epoch value.
        :type epoch: int
        """
        self.offset = None
        self._callback = None
        self.prefetch = prefetch
        self.epoch = epoch
        self.delivered = 0
        properties = None
        if epoch:
            properties = {types.AMQPSymbol(self._epoch): types.AMQPLong(int(epoch))}
        self._handler = ReceiveClient(
            source,
            auth=client.auth,
            debug=client.debug,
            prefetch=self.prefetch,
            link_properties=properties,
            timeout=self.timeout)

    @property
    def queue_size(self):
        """
        The current size of the unprocessed message queue.
        :returns: int
        """
        if self._handler._received_messages:
            return self._handler._received_messages.qsize()
        return 0

    def on_message(self, event):
        """
        Callback to process a received message and wrap it in EventData.
        Will also call a user supplied callback.
        :param event: The received message.
        :type event: ~uamqp.Message
        :returns: ~azure.eventhub.EventData.
        """
        self.delivered += 1
        event_data = EventData.create(event)
        if self._callback:
            self._callback(event_data)
        self.offset = event_data.offset
        return event_data

    def receive(self, max_batch_size=None, callback=None, timeout=None):
        """
        Receive events from the EventHub.
        :param max_batch_size: Receive a batch of events. Batch size will
         be up to the maximum specified, but will return as soon as service
         returns no new events. If combined with a timeout and no events are
         retrieve before the time, the result will be empty. If no batch
         size is supplied, returned generator will continue to iterate over
         received events indefinitely (or until timeout reached.).
        :type max_batch_size: int
        :param callback: A callback to be run for each received event. This must
         be a function that accepts a single argument - the event data. This callback
         will be run before the message is returned in the result generator.
        :type callback: func[~azure.eventhub.EventData]
        :returns: Generator[~azure.eventhub.EventData]
        """
        timeout_ms = 1000 * timeout if timeout else 0
        self._callback = callback
        if max_batch_size:
            message_iter = self._handler.receive_message_batch(
                max_batch_size=max_batch_size,
                on_message_received=self.on_message,
                timeout=timeout_ms)
            for event_data in message_iter:
                yield event_data
        else:
            receive_timeout = time.time() + timeout if timeout else None
            message_iter = self._handler.receive_message_batch(
                on_message_received=self.on_message,
                timeout=timeout_ms)
            while message_iter and (not receive_timeout or time.time() < receive_timeout):
                for event_data in message_iter:
                    yield event_data
                if receive_timeout:
                    timeout_ms = int((receive_timeout - time.time()) * 1000)
                message_iter = self._handler.receive_message_batch(
                    on_message_received=self.on_message,
                    timeout=timeout_ms)

    def selector(self, default):
        """
        Create a selector for the current offset if it is set.
        :param default: The fallback receive offset.
        :type default: ~azure.eventhub.Offset
        :returns: ~azure.eventhub.Offset
        """
        if self.offset is not None:
            return Offset(self.offset).selector()
        return default


class EventData(object):
    """
    The EventData class is a holder of event content.
    Acts as a wrapper to an ~uamqp.Message object.
    """

    PROP_SEQ_NUMBER = b"x-opt-sequence-number"
    PROP_OFFSET = b"x-opt-offset"
    PROP_PARTITION_KEY = b"x-opt-partition-key"

    def __init__(self, body=None, batch=None):
        """
        Initialize EventData
        :param body: The data to send in a single message.
        :type body: str or bytes
        :param batch: A data generator to send batched messages.
        :type batch: Generator
        """
        if batch:
            self.message = BatchMessage(data=batch, multi_messages=True)
        elif body:
            self.message = Message(body)
        self._annotations = {}
        self._properties = {}

    @property
    def sequence_number(self):
        """
        The sequence number of the event data object.
        :returns: int
        """
        return self._annotations.get(EventData.PROP_SEQ_NUMBER, None)

    @property
    def offset(self):
        """
        The offset of the event data object.
        :returns: int
        """
        return self._annotations.get(EventData.PROP_OFFSET, None)

    @property
    def partition_key(self):
        """
        The partition key of the event data object.
        :returns: bytes
        """
        return self._annotations.get(EventData.PROP_PARTITION_KEY, None)

    @partition_key.setter
    def partition_key(self, value):
        """
        Set the partition key of the event data object.
        :param value: The partition key to set.
        :type value: str or bytes
        """
        annotations = dict(self._annotations)
        annotations[types.AMQPSymbol(EventData.PROP_PARTITION_KEY)] = value
        self.message.message_annotations = annotations
        self._annotations = annotations

    @property
    def properties(self):
        """
        Application defined properties on the message.
        :returns: dict
        """
        return self._properties

    @property
    def body(self):
        """
        The body of the event data object.
        :returns: bytes or generator[bytes]
        """
        return self.message.get_data()

    @classmethod
    def create(cls, message):
        """
        Creates an event data object from an AMQP message.
        :param message: The received message.
        :type message: ~uamqp.Message
        """
        event_data = EventData()
        event_data.message = message
        event_data._annotations = message.message_annotations
        event_data._properties = message.application_properties
        return event_data


class Offset(object):
    """
    The offset (position or timestamp) where a receiver starts. Examples:
    Beginning of the event stream:
      >>> offset = Offset("-1")
    End of the event stream:
      >>> offset = Offset("@latest")
    Events after the specified offset:
      >>> offset = Offset("12345")
    Events from the specified offset:
      >>> offset = Offset("12345", True)
    Events after current time:
      >>> offset = Offset(datetime.datetime.utcnow())
    Events after a specific timestmp:
      >>> offset = Offset(timestamp(1506968696002))
    """

    def __init__(self, value, inclusive=False):
        """
        Initialize Offset.
        :param value: The offset value.
        :type value: ~datetime.datetime or int or str
        :param inclusive: Whether to include the supplied value as the start point.
        :type inclusive: bool
        """
        self.value = value
        self.inclusive = inclusive

    def selector(self):
        """
        Creates a selector expression of the offset.
        :returns: bytes
        """
        if isinstance(self.value, datetime.datetime):
            epoch = datetime.datetime.utcfromtimestamp(0)
            milli_seconds = timestamp((self.value - epoch).total_seconds() * 1000.0)  # TODO
            return ("amqp.annotation.x-opt-enqueued-time > '{}'".format(milli_seconds)).encode('utf-8')
        elif isinstance(self.value, int):
            return ("amqp.annotation.x-opt-enqueued-time > '{}'".format(self.value)).encode('utf-8')
        else:
            operator = ">=" if self.inclusive else ">"
            return ("amqp.annotation.x-opt-offset {} '{}'".format(operator, self.value)).encode('utf-8')


class EventHubError(Exception):
    """
    Represents an error happened in the client.
    """
    pass
