from twisted.application.service import Service
from twisted.internet import reactor
from twisted.internet.defer import Deferred, DeferredList
from twisted.internet.protocol import ReconnectingClientFactory
from twisted.protocols.basic import Int32StringReceiver
from carbon.conf import settings
from carbon.util import pickle
from carbon import log, state, instrumentation
from collections import deque
from time import time
import json
import os


SEND_QUEUE_LOW_WATERMARK = settings.MAX_QUEUE_SIZE * settings.QUEUE_LOW_WATERMARK_PCT


class SpoolingCarbonClientProtocol(Int32StringReceiver):
  def connectionMade(self):
    log.clients("%s::connectionMade" % self)
    self.paused = False
    self.connected = True
    self.transport.registerProducer(self, streaming=True)
    # Define internal metric names
    self.lastResetTime = time()

    self.factory.connectionMade.callback(self)
    self.factory.connectionMade = Deferred()
    self.sendQueued()

  def connectionLost(self, reason):
    """Monitor the state of the connection - this is useful
    instrumentation data, but should not be used to block writes to the
    spool.
    """
    log.clients("%s::connectionLost %s" % (self, reason.getErrorMessage()))
    self.connected = False

  def pauseProducing(self):
    """XXX self.paused should be ignored for the purposes of writing to the spool."""
    self.paused = False

  def resumeProducing(self):
    self.paused = False
    self.sendQueued()

  def stopProducing(self):
    self.disconnect()

  def disconnect(self):
    if self.connected:
      self.transport.unregisterProducer()
      self.transport.loseConnection()
      self.connected = False

  def sendDatapoint(self, metric, datapoint):
    self.factory.enqueue(metric, datapoint)
    reactor.callLater(settings.TIME_TO_DEFER_SENDING, self.sendQueued)

  def _sendDatapoints(self, datapoints):
      """Once some number of datapoints have been accumulated, write
      them to a file, and that file will be delivered to the remote
      end.

      The format of the file is repr, so we'll eval the file, line by line
      and send the file.  Should be cheaper and easier than using multiple
      pickles.
      """
      # self.sendString(pickle.dumps(datapoints, protocol=-1))
      self.factory.queue_file.write(json.dumps(datapoints) + "\n")
      # XXX Change "sent" to "written"?
      instrumentation.increment(self.factory.sent, len(datapoints))
      instrumentation.increment(self.factory.batchesSent)
      self.factory.checkQueue()

  def sendQueued(self):
    """This should be the only method that will be used to send stats.
    In order to not hold the event loop and prevent stats from flowing
    in while we send them out, this will process
    settings.MAX_DATAPOINTS_PER_MESSAGE stats, write them to the
    queue, and if there are still items in the queue, this will invoke
    reactor.callLater to schedule another run of sendQueued after a
    very short wait.

    When spooling, the MAX_DATAPOINTS_PER_MESSAGE will determine how
    many metrics will be put per line of a file, and that can be
    naively used as a batch size.  Something more sophisticated can be
    done as well, but doesn't need to be if the batch size used makes
    sense.
    """
    chained_invocation_delay = 0.0001
    queueSize = self.factory.queueSize

    instrumentation.max(self.factory.relayMaxQueueLength, queueSize)
    if not self.factory.hasQueuedDatapoints():
      return

    if time() >= self.factory.next_flush_time:
        self.factory.set_next_flush_time()
        self.factory.open_next_queue_file()
    self._sendDatapoints(self.factory.takeSomeFromQueue())
    if (self.factory.queueFull.called and
        queueSize < SEND_QUEUE_LOW_WATERMARK):
      self.factory.queueHasSpace.callback(queueSize)
    if self.factory.hasQueuedDatapoints():
      reactor.callLater(chained_invocation_delay, self.sendQueued)

  def __str__(self):
    return 'SpoolingCarbonClientProtocol(%s:%d:%s)' % (self.factory.destination)
  __repr__ = __str__


class SpoolingCarbonClientFactory(ReconnectingClientFactory):
  maxDelay = 5

  def __init__(self, destination):
    self.destination = destination
    self.destinationName = ('%s:%d:%s' % destination).replace('.', '_')
    self.host, self.port, self.carbon_instance = destination
    self.addr = (self.host, self.port)
    self.started = False
    # This factory maintains protocol state across reconnects
    self.queue = deque() # Change to make this the sole source of metrics to be sent.
    self.connectedProtocol = None
    self.queueEmpty = Deferred()
    self.queueFull = Deferred()
    self.queueFull.addCallback(self.queueFullCallback)
    self.queueHasSpace = Deferred()
    self.queueHasSpace.addCallback(self.queueSpaceCallback)
    self.connectFailed = Deferred()
    self.connectionMade = Deferred()
    self.connectionLost = Deferred()
    # Define internal metric names
    self.queuedUntilReady = 'destinations.%s.queuedUntilReady' % self.destinationName
    self.sent = 'destinations.%s.sent' % self.destinationName
    self.relayMaxQueueLength = 'destinations.%s.relayMaxQueueLength' % self.destinationName
    self.batchesSent = 'destinations.%s.batchesSent' % self.destinationName

    self.attemptedRelays = 'destinations.%s.attemptedRelays' % self.destinationName
    self.fullQueueDrops = 'destinations.%s.fullQueueDrops' % self.destinationName
    self.queuedUntilConnected = 'destinations.%s.queuedUntilConnected' % self.destinationName
    # XXX create the temp dir if it doesn't already exist
    self.send_tmp_dir = "{0}/temp/{1}:{2}".format(
        settings.SPOOLING_PATH, self.host, self.port)
    self.send_queue_dir = "{0}/send/{1}:{2}".format(
        settings.SPOOLING_PATH, self.host, self.port)
    self.queue_file_prefix = "{0}/send".format(self.send_queue_dir)
    self.queue_file = None
    self.open_next_queue_file()
    # self.sec_between_flushes = settings.FLUSH_INTERVAL # seconds


  @property
  def next_flush_time(self):
    """Get the next time, and set it if it's currently None"""
    try:
        if self._next_flush_time:
            pass
    except AttributeError:
        self._next_flush_time = time() + settings.FLUSH_INTERVAL
    return self._next_flush_time

  @next_flush_time.setter
  def next_flush_time(self, next_time):
    """This is here in case someone wants to run self.next_flush_time
    = some_number.  I don't want to leave some poor programmer
    overwriting the next_flush_time() method with "1" or something.
    """
    self.set_next_flush_time(next_time)

  def set_next_flush_time(self, next_time=None):
    """If a manual time is desired, set it - this is not an increment
    over time.time() (aka "now"), this is the actual time at which the
    flush should happen as seconds since the epoch.
    """
    if next_time is None:
      self._next_flush_time = time() + settings.FLUSH_INTERVAL
    else:
      self._next_flush_time = next_time

  def open_next_queue_file(self):
      """While running this method contains the only operations that
      will be run on the queue file. Opening the file this way will
      close the old one and do whatever is necessary - either re-name
      it if there is data, or remove it if there is no data.
      """
      if self.queue_file:
          size = self.queue_file.tell() # should be at the end of the file
          self.queue_file.close()
          fname = os.path.basename(self.queue_file_name)
          new_name = "{0}/{1}.json".format(self.send_queue_dir, fname)
          log.clients("{0}::open_next_queue_file new_name is {1}".format(self, new_name))

          try:
              if size == 0:
                  os.unlink(self.queue_file_name)
              else:
                  os.rename(self.queue_file_name, new_name) # Tidy up
          except IOError:
              # in case it was deleted by hand, no crying over spilt milk
              # https://github.com/pcn/carbon/issues/15
              pass

      self.queue_file_name = "{0}/{1:.2f}".format(self.send_tmp_dir, self.next_flush_time)
      self.queue_file = open(self.queue_file_name, 'w')


  def queueFullCallback(self, result):
    state.events.cacheFull()
    log.clients('%s send queue is full (%d datapoints)' % (self, result))

  def queueSpaceCallback(self, result):
    if self.queueFull.called:
      log.clients('%s send queue has space available' % self.connectedProtocol)
      self.queueFull = Deferred()
      self.queueFull.addCallback(self.queueFullCallback)
      state.events.cacheSpaceAvailable()
    self.queueHasSpace = Deferred()
    self.queueHasSpace.addCallback(self.queueSpaceCallback)

  def buildProtocol(self, addr):
    self.resetDelay()
    self.connectedProtocol = SpoolingCarbonClientProtocol()
    self.connectedProtocol.factory = self
    return self.connectedProtocol

  def startConnecting(self): # calling this startFactory yields recursion problems
    self.started = True
    self.connector = reactor.connectTCP(self.host, self.port, self)

  def stopConnecting(self):
    self.started = False
    self.stopTrying()
    if self.connectedProtocol and self.connectedProtocol.connected:
      return self.connectedProtocol.disconnect()

  @property
  def queueSize(self):
    return len(self.queue)

  def hasQueuedDatapoints(self):
    return bool(self.queue)

  def takeSomeFromQueue(self):
    """Use self.queue, which is a collections.deque, to pop up to
    settings.MAX_DATAPOINTS_PER_MESSAGE items from the left of the
    queue.
    """
    def yield_max_datapoints():
      for count in range(settings.MAX_DATAPOINTS_PER_MESSAGE):
        try:
          yield self.queue.popleft()
        except IndexError:
          raise StopIteration
    return list(yield_max_datapoints())

  def checkQueue(self):
    """Check if the queue is empty. If the queue isn't empty or
    doesn't exist yet, then this will invoke the callback chain on the
    self.queryEmpty Deferred chain with the argument 0, and will
    re-set the queueEmpty callback chain with a new Deferred
    object.
    """
    if not self.queue:
      self.queueEmpty.callback(0)
      self.queueEmpty = Deferred()

  def enqueue(self, metric, datapoint):
    self.queue.append((metric, datapoint))

  def enqueue_from_left(self, metric, datapoint):
    self.queue.appendleft((metric, datapoint))

  def sendDatapoint(self, metric, datapoint):
    instrumentation.increment(self.attemptedRelays)
    if self.queueSize >= settings.MAX_QUEUE_SIZE:
      if not self.queueFull.called:
        self.queueFull.callback(self.queueSize)
      instrumentation.increment(self.fullQueueDrops)
    else:
      self.enqueue(metric, datapoint)

    if self.connectedProtocol:
      reactor.callLater(settings.TIME_TO_DEFER_SENDING, self.connectedProtocol.sendQueued)
    else:
      instrumentation.increment(self.queuedUntilConnected)

  def sendHighPriorityDatapoint(self, metric, datapoint):
    """The high priority datapoint is one relating to the carbon
    daemon itself.  It puts the datapoint on the left of the deque,
    ahead of other stats, so that when the carbon-relay, specifically,
    is overwhelmed its stats are more likely to make it through and
    expose the issue at hand.

    In addition, these stats go on the deque even when the max stats
    capacity has been reached.  This relies on not creating the deque
    with a fixed max size.
    """
    instrumentation.increment(self.attemptedRelays)
    self.enqueue_from_left(metric, datapoint)

    if self.connectedProtocol:
      reactor.callLater(settings.TIME_TO_DEFER_SENDING, self.connectedProtocol.sendQueued)
    else:
      instrumentation.increment(self.queuedUntilConnected)

  def startedConnecting(self, connector):
    log.clients("%s::startedConnecting (%s:%d)" % (self, connector.host, connector.port))

  def clientConnectionLost(self, connector, reason):
    ReconnectingClientFactory.clientConnectionLost(self, connector, reason)
    log.clients("%s::clientConnectionLost (%s:%d) %s" % (self, connector.host, connector.port, reason.getErrorMessage()))
    self.connectedProtocol = None
    self.connectionLost.callback(0)
    self.connectionLost = Deferred()

  def clientConnectionFailed(self, connector, reason):
    ReconnectingClientFactory.clientConnectionFailed(self, connector, reason)
    log.clients("%s::clientConnectionFailed (%s:%d) %s" % (self, connector.host, connector.port, reason.getErrorMessage()))
    self.connectFailed.callback(dict(connector=connector, reason=reason))
    self.connectFailed = Deferred()

  def disconnect(self):
    self.queueEmpty.addCallback(lambda result: self.stopConnecting())
    readyToStop = DeferredList(
      [self.connectionLost, self.connectFailed],
      fireOnOneCallback=True,
      fireOnOneErrback=True)
    self.checkQueue()

    # This can happen if the client is stopped before a connection is ever made
    if (not readyToStop.called) and (not self.started):
      readyToStop.callback(None)

    return readyToStop

  def __str__(self):
    return 'CarbonClientFactory(%s:%d:%s)' % self.destination
  __repr__ = __str__


class CarbonClientManager(Service):
  def __init__(self, router):
    self.router = router
    self.client_factories = {} # { destination : CarbonClientFactory() }

  def startService(self):
    Service.startService(self)
    for factory in self.client_factories.values():
      if not factory.started:
        factory.startConnecting()

  def stopService(self):
    Service.stopService(self)
    self.stopAllClients()

  def startClient(self, destination):
    if destination in self.client_factories:
      return

    log.clients("connecting to carbon daemon at %s:%d:%s" % destination)
    self.router.addDestination(destination)
    factory = self.client_factories[destination] = SpoolingCarbonClientFactory(destination)
    connectAttempted = DeferredList(
        [factory.connectionMade, factory.connectFailed],
        fireOnOneCallback=True,
        fireOnOneErrback=True)
    if self.running:
      factory.startConnecting() # this can trigger & replace connectFailed

    return connectAttempted

  def stopClient(self, destination):
    factory = self.client_factories.get(destination)
    if factory is None:
      return

    self.router.removeDestination(destination)
    stopCompleted = factory.disconnect()
    stopCompleted.addCallback(lambda result: self.disconnectClient(destination))
    return stopCompleted

  def disconnectClient(self, destination):
    factory = self.client_factories.pop(destination)
    c = factory.connector
    if c and c.state == 'connecting' and not factory.hasQueuedDatapoints():
      c.stopConnecting()

  def stopAllClients(self):
    deferreds = []
    for destination in list(self.client_factories):
      deferreds.append( self.stopClient(destination) )
    return DeferredList(deferreds)

  def sendDatapoint(self, metric, datapoint):
    for destination in self.router.getDestinations(metric):
      self.client_factories[destination].sendDatapoint(metric, datapoint)

  def sendHighPriorityDatapoint(self, metric, datapoint):
    for destination in self.router.getDestinations(metric):
      self.client_factories[destination].sendHighPriorityDatapoint(metric, datapoint)

  def __str__(self):
    return "<%s[%x]>" % (self.__class__.__name__, id(self))
