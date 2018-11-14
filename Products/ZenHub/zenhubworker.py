#! /usr/bin/env python
##############################################################################
#
# Copyright (C) Zenoss, Inc. 2008, 2018 all rights reserved.
#
# This content is made available according to terms specified in
# License.zenoss under the directory where your Zenoss product is installed.
#
##############################################################################

import logging
import os
import signal
import time

from collections import defaultdict
from contextlib import contextmanager
from optparse import SUPPRESS_HELP

from metrology import Metrology
from twisted.application.internet import ClientService, backoffPolicy
from twisted.cred.credentials import UsernamePassword
from twisted.internet.endpoints import clientFromString
from twisted.internet import defer, reactor, error
from twisted.spread import pb

import Globals

from Products.DataCollector.plugins import DataMaps
from Products.DataCollector.Plugins import loadPlugins
from Products.ZenHub import PB_PORT
from Products.ZenHub.metricmanager import MetricManager
from Products.ZenHub.servicemanager import (
    HubServiceRegistry, UnknownServiceError
)
from Products.ZenHub.PBDaemon import RemoteConflictError, RemoteBadMonitor
from Products.ZenUtils.debugtools import ContinuousProfiler
from Products.ZenUtils.Time import isoDateTime
from Products.ZenUtils.Utils import unused, zenPath, atomicWrite
from Products.ZenUtils.ZCmdBase import ZCmdBase

unused(Globals, DataMaps)

IDLE = "None/None"


def getLogger(cls):
    name = "zen.zenhubworker.%s" % (cls.__name__)
    return logging.getLogger(name)


class ZenHubWorker(ZCmdBase, pb.Referenceable):
    """Execute ZenHub requests.
    """

    mname = name = "zenhubworker"

    def __init__(self):
        ZCmdBase.__init__(self)

        if self.options.profiling:
            self.profiler = ContinuousProfiler('ZenHubWorker', log=self.log)
            self.profiler.start()
            reactor.addSystemEventTrigger(
                'before', 'shutdown', self.profiler.stop
            )

        self.instanceId = self.options.workerid
        self.current = IDLE
        self.currentStart = 0
        self.numCalls = Metrology.meter("zenhub.workerCalls")

        self.zem = self.dmd.ZenEventManager
        loadPlugins(self.dmd)

        serviceFactory = ServiceReferenceFactory(self)
        self.__registry = HubServiceRegistry(self.dmd, serviceFactory)

        # Configure/initialize the ZenHub client
        creds = UsernamePassword(
            self.options.hubusername, self.options.hubpassword
        )
        endpointDescriptor = "tcp:{host}:{port}".format(
            host=self.options.hubhost, port=self.options.hubport
        )
        endpoint = clientFromString(reactor, endpointDescriptor)
        self.__client = ZenHubClient(reactor, endpoint, creds, self)

        # Setup Metric Reporting
        self.log.debug("Creating async MetricReporter")
        self._metric_manager = MetricManager(
            daemon_tags={
                'zenoss_daemon': 'zenhub_worker_%s' % self.options.workerid,
                'zenoss_monitor': self.options.monitor,
                'internal': True
            })

    def start(self, reactor):
        """
        """
        self.log.debug("establishing SIGUSR1 signal handler")
        signal.signal(signal.SIGUSR1, self.sighandler_USR1)
        self.log.debug("establishing SIGUSR2 signal handler")
        signal.signal(signal.SIGUSR2, self.sighandler_USR2)

        self.__client.start()
        reactor.addSystemEventTrigger(
            'before', 'shutdown', self.__client.stop
        )

        self._metric_manager.start()
        reactor.addSystemEventTrigger(
            'before', 'shutdown', self._metric_manager.stop
        )

        reactor.addSystemEventTrigger("after", "shutdown", self.reportStats)

    def audit(self, action):
        """ZenHubWorkers restart all the time, it is not necessary to
        audit log it.
        """
        pass

    def setupLogging(self):
        """Override setupLogging to add instance id/count information to
        all log messages.
        """
        super(ZenHubWorker, self).setupLogging()
        instanceInfo = "(%s)" % (self.options.workerid,)
        template = (
            "%%(asctime)s %%(levelname)s %%(name)s: %s %%(message)s"
        ) % instanceInfo
        rootLog = logging.getLogger()
        formatter = logging.Formatter(template)
        for handler in rootLog.handlers:
            handler.setFormatter(formatter)

    def sighandler_USR1(self, signum, frame):
        try:
            if self.options.profiling:
                self.profiler.dump_stats()
            super(ZenHubWorker, self).sighandler_USR1(signum, frame)
        except Exception:
            pass

    def sighandler_USR2(self, *args):
        try:
            self.reportStats()
        except Exception:
            pass

    def work_started(self, startTime):
        self.currentStart = startTime
        self.numCalls.mark()

    def work_finished(self, duration, method):
        self.log.debug("Time in %s: %.2f", method, duration)
        self.current = IDLE
        self.currentStart = 0
        if self.numCalls.count >= self.options.call_limit:
            reactor.callLater(0, self._shutdown)

    def reportStats(self):
        now = time.time()
        if self.current != IDLE:
            self.log.info(
                "Currently performing %s, elapsed %.2f s",
                self.current, now-self.currentStart
            )
        else:
            self.log.info("Currently IDLE")
        if self.__registry:
            loglines = ["Running statistics:"]
            sorted_data = sorted(
                self.__registry.iteritems(),
                key=lambda kvp: (kvp[0][1], kvp[0][0].rpartition('.')[-1])
            )
            for svc, svcob in sorted_data:
                svc = "%s/%s" % (svc[1], svc[0].rpartition('.')[-1])
                for method, stats in sorted(svcob.callStats.items()):
                    loglines.append(
                        " - %-48s %-32s %8d %12.2f %8.2f %s" % (
                            svc, method,
                            stats.numoccurrences,
                            stats.totaltime,
                            stats.totaltime/stats.numoccurrences
                            if stats.numoccurrences else 0.0,
                            isoDateTime(stats.lasttime)
                        )
                    )
            self.log.info('\n'.join(loglines))
        else:
            self.log.info("no service activity statistics")

    def remote_reportStatus(self):
        """Remote command to report the worker's current status to the log.
        """
        try:
            self.reportStats()
        except Exception:
            self.log.exception("Failed to report status")

    def remote_getService(self, name, monitor):
        """Return a reference to the named service.

        @param name {str} Name of the service to load
        @param monitor {str} Name of the collection monitor
        """
        try:
            return self.__registry.getService(name, monitor)
        except RemoteBadMonitor:
            # Catch and rethrow this Exception dervived exception.
            raise
        except UnknownServiceError:
            self.log.error("Service '%s' not found", name)
            raise
        except Exception as ex:
            self.log.exception("Failed to get service '%s'", name)
            raise pb.Error(str(ex))

    def _shutdown(self):
        self.log.info("Shutting down")
        try:
            reactor.stop()
        except error.ReactorNotRunning:
            pass

    def buildOptions(self):
        """Options, mostly to find where zenhub lives
        These options should be passed (by file) from zenhub.
        """
        ZCmdBase.buildOptions(self)
        self.parser.add_option(
            '--hubhost', dest='hubhost', default='localhost',
            help="Host to use for connecting to ZenHub"
        )
        self.parser.add_option(
            '--hubport', dest='hubport', type='int', default=PB_PORT,
            help="Port to use for connecting to ZenHub"
        )
        self.parser.add_option(
            '--hubusername', dest='hubusername', default='admin',
            help="Login name to use when connecting to ZenHub"
        )
        self.parser.add_option(
            '--hubpassword', dest='hubpassword', default='zenoss',
            help="password to use when connecting to ZenHub"
        )
        self.parser.add_option(
            '--call-limit', dest='call_limit', type='int', default=200,
            help="Maximum number of remote calls before restarting worker"
        )
        self.parser.add_option(
            '--profiling', dest='profiling',
            action='store_true', default=False, help="Run with profiling on"
        )
        self.parser.add_option(
            '--monitor', dest='monitor', default='localhost',
            help='Name of the performance monitor this hub runs on'
        )
        self.parser.add_option(
            '--workerid', dest='workerid', type='int', default=0,
            help=SUPPRESS_HELP
        )


class ZenHubClient(object):
    """Implements a client for connecting to ZenHub as a ZenHub Worker.
    """

    def __init__(self, reactor, endpoint, credentials, worker):
        """Initializes a ZenHubClient instance.

        @param reactor {IReactorCore}
        @param endpoint {IStreamClientEndpoint} Where zenhub is found
        @param credentials {IUsernamePassword} Credentials to log into ZenHub.
        @param worker {IReferenceable} Reference to worker
        """
        self.__log = getLogger(type(self))
        self.__credentials = credentials
        self.__worker = worker
        self.__stopping = False
        factory = pb.PBClientFactory()
        self.__service = ClientService(
            endpoint, factory,
            retryPolicy=backoffPolicy(initialDelay=0.5, factor=3.0)
        )

    def start(self):
        self.__service.startService()
        self.__prepForConnection()

    def stop(self):
        self.__stopping = True
        self.__service.stopService()

    def __prepForConnection(self):
        if not self.__stopping:
            self.__log.info("Prepping for connection")
            self.__service.whenConnected().addCallback(self.__connected)

    def __disconnected(self, *args):
        """Called when the connection to ZenHub is lost.

        Ensures that processing resumes when the connection to ZenHub
        is restored.
        """
        self.__log.info("Lost connection to ZenHub: %r", args)
        self.__prepForConnection()

    @defer.inlineCallbacks
    def __connected(self, broker):
        """Called when a connection to ZenHub is established.

        Logs into ZenHub and passes up a worker reference for ZenHub
        to use to dispatch method calls.
        """
        self.__log.info("Connection to ZenHub established")
        try:
            filename = "zenhub_connected"
            signalFilePath = zenPath('var', filename)

            zenhub = yield broker.factory.login(
                self.__credentials, self.__worker
            )
            yield zenhub.callRemote(
                "reportingForWork",
                self.__worker, workerId=self.__worker.instanceId
            )
        except Exception as ex:
            self.__log.error("Unable to report for work: %s", ex)
            self.__log.debug('Removing file at %s', signalFilePath)
            try:
                os.remove(signalFilePath)
            except Exception:
                pass
            reactor.stop()
        else:
            self.__log.info("Logged into ZenHub")
            self.__log.debug('Writing file at %s', signalFilePath)
            atomicWrite(signalFilePath, '')

            # Connection complete; install a listener to be notified if
            # the connection is lost.
            broker.notifyOnDisconnect(self.__disconnected)


class ServiceReferenceFactory(object):
    """This is a factory that builds ServiceReference objects.
    """

    def __init__(self, worker):
        """Initializes an instance of ServiceReferenceFactory.

        @param worker {ZenHubWorker}
        """
        self.__worker = worker

    def build(self, service, name, monitor):
        """Build and return a ServiceReference object.

        @param service {HubService derived} Service object
        @param name {string} Name of the service
        @param monitor {string} Name of the performance monitor (collector)
        """
        return ServiceReference(service, name, monitor, self.__worker)


class ServiceReference(pb.Referenceable):
    """
    """

    def __init__(self, service, name, monitor, worker):
        """
        """
        self.__name = name
        self.__monitor = monitor
        self.__service = service
        self.__worker = worker
        self.callStats = defaultdict(_CumulativeWorkerStats)
        self.debug = False

    @property
    def name(self):
        return self.__name

    @property
    def monitor(self):
        return self.__monitor

    @defer.inlineCallbacks
    def remoteMessageReceived(self, broker, message, args, kw):
        """
        """
        with self.__update_stats(message):
            for i in range(4):
                try:
                    yield self.__worker.async_syncdb()
                except RemoteConflictError:
                    pass

                try:
                    result = yield self.__service.remoteMessageReceived(
                        broker, message, args, kw
                    )
                    defer.returnValue(result)
                except RemoteConflictError:
                    pass  # continue
            else:
                result = yield self.__service.remoteMessageReceived(
                    broker, message, args, kw
                )
                defer.returnValue(result)

    @contextmanager
    def __update_stats(self, method):
        """
        @param method {str} Remote method name on service.
        """
        try:
            name = self.__name.rpartition('.')[-1]
            self.__worker.current = "%s/%s" % (name, method)
            start = time.time()
            self.__worker.work_started(start)
            yield self.__service
        finally:
            finish = time.time()
            secs = finish - start
            self.callStats[method].addOccurrence(secs, finish)
            self.__service.callTime += secs
            self.__worker.work_finished(secs, method)


class _CumulativeWorkerStats(object):
    """
    Internal class for maintaining cumulative stats on frequency and runtime
    for individual methods by service
    """
    def __init__(self):
        self.numoccurrences = 0
        self.totaltime = 0.0
        self.lasttime = 0

    def addOccurrence(self, elapsed, now=None):
        if now is None:
            now = time.time()
        self.numoccurrences += 1
        self.totaltime += elapsed
        self.lasttime = now


if __name__ == '__main__':
    zhw = ZenHubWorker()
    zhw.start(reactor)
    reactor.run()
