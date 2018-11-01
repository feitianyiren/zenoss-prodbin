##############################################################################
#
# Copyright (C) Zenoss, Inc. 2018, all rights reserved.
#
# This content is made available according to terms specified in
# License.zenoss under the directory where your Zenoss product is installed.
#
##############################################################################

import collections
import logging
import time

from twisted.internet import defer
from twisted.spread import pb
from zope.interface import implementer, Interface, Attribute

from .PBDaemon import RemoteException
from .worklist import get_worklist_metrics


class IAsyncDispatch(Interface):
    """Interface for classes that implement code that can execute jobs
    on hub services.

    The 'routes' attribute should be a sequence of service/method pairs
    the identify the services and methods an IAsyncDispatch implementation
    supports.  E.g.

        routes = (
            ("EventService", "sendEvent"),
            ("EventService", "sendEvents"),
        )

    is what a dispatcher would declare to specify that it handles jobs
    that want to execute the sendEvent and sendEvents methods on the
    EventService service.
    """

    routes = Attribute("Sequence of service-name/method-name pairs.")

    def submit(job):
        """Asynchronously executes the job.

        @param job {ServiceCallJob} job to execute
        @returns {Deferred}
        """


@implementer(IAsyncDispatch)
class DispatchingExecutor(object):
    """An dispatcher that maps service/method calls to other dispatchers.

    DispatchingExecutor maintains a registry of dispatchers that maps
    service/method calls to specific dispatcher instances.
    """

    def __init__(self, dispatchers=None, default=None):
        """Initialize a DispatchingExecutor instance.

        @param dispatchers {[IAsyncDispatch,]} Sequence of dispatchers
        @param default {IAsyncDispatch} Default dispatcher if the registry
            doesn't contain an appropriate dispatcher.
        """
        self.__registry = {}
        self.__default = default
        if dispatchers is None:
            dispatchers = []
        elif not isinstance(dispatchers, collections.Sequence):
            dispatchers = [dispatchers]
        for dispatcher in dispatchers:
            self.register(dispatcher)

    @property
    def default(self):
        """Return the default dispatcher.
        """
        return self.__default

    @default.setter
    def default(self, dispatcher):
        """Sets the default dispatcher
        """
        self.__default = dispatcher

    @property
    def dispatchers(self):
        """Returns a sequence containing the registered dispatchers.

        The returned sequence does not include the default dispatcher.
        """
        return tuple(self.__registry.values())

    def register(self, dispatcher):
        """Register a dispatcher for a service and method.

        A ValueError exception is raised if the given dispatcher tries to
        map a service/method route already mapped by another dispatcher.

        @param dispatcher {IAsyncDispatch} dispatcher to register
        """
        entries = {
            (service, method): dispatcher
            for service, method in dispatcher.routes
        }
        already_defined = set(entries) & set(self.__registry)
        if already_defined:
            raise ValueError(
                "Dispatcher already defined on route(s): %s", ', '.join(
                    "('%s', '%s')" % r for r in already_defined
                )
            )

        self.__registry.update(entries)

    @defer.inlineCallbacks
    def submit(self, job):
        """Submits a job for execution.

        Returns a deferred that will fire when execution completes.
        """
        dispatcher = self.__registry.get(
            (job.service, job.method), self.__default
        )
        if dispatcher is None:
            raise RuntimeError(
                "No dispatcher found that can execute method '%s' on "
                "service '%s'" % (job.method, job.service)
            )
        state = yield dispatcher.submit(job)
        defer.returnValue(state)


@implementer(IAsyncDispatch)
class EventDispatcher(object):
    """An executor that executes sendEvent and sendEvents methods
    on the EventService service.
    """

    routes = (
        ("EventService", "sendEvent"),
        ("EventService", "sendEvents"),
    )

    def __init__(self, eventmanager):
        """Initializes an EventDispatcher instance.

        @param eventmanager {dmd.ZenEventManager} the event manager
        """
        self.__zem = eventmanager

    def submit(self, job):
        """Submits a job for execution.

        Returns a deferred that will fire when execution completes.
        """
        method = getattr(self.__zem, job.method, None)
        if method is None:
            return defer.fail(AttributeError(
                "No method named '%s' on '%s'" % (job.method, job.service)
            ))
        try:
            state = method(*job.args, **job.kwargs)
        except Exception as ex:
            return defer.fail(ex)
        else:
            return defer.succeed(state)


@implementer(IAsyncDispatch)
class WorkerPoolDispatcher(object):
    """An executor that executes service/method calls on remote workers.

    The WorkerPoolDispatcher serves as the 'default' dispatcher for the
    DispatchingExecutor executor.
    """

    # No routes needed for default dispatcher
    routes = ()

    def __init__(self, reactor, worklist):
        """Initializes a WorkerPoolDispatcher instance.
        """
        self.__reactor = reactor
        self.__worklist = worklist

        # Map of workers to remote service references.
        #
        # workers = {
        #    worker: {
        #       (serviceName, monitor): pb.RemoteReference,
        #       ...
        #    },
        #    ...
        # }
        self.__workers = {}
        self.__workTracker = {}
        self.__executionTimer = collections.defaultdict(ExecutionStats)

        self.__executeListeners = []

        self.__log = logging.getLogger("zen.zenhub")

    @property
    def worklist(self):
        return self.__worklist

    def getStats(self):
        gauges = get_worklist_metrics(self.__worklist)
        return gauges, self.__workTracker, self.__executionTimer

    def onExecute(self, listener):
        """Register a listener that will be called prior the execution of
        a job on a worker.

        @param listener {callable}
        """
        self.__executeListeners.append(listener)

    def add(self, worker):
        """Add a worker to the pool.
        """
        if worker not in self.__workers:
            self.__workers[worker] = {}

    def remove(self, worker):
        """Remove a worker from the pool.
        """
        if worker in self.__workers:
            del self.__workers[worker]

    def __contains__(self, worker):
        """Returns True if worker is registered, else False is returned.
        """
        return worker in self.__workers

    @defer.inlineCallbacks
    def reportWorkerStatus(self):
        """Instructs workers to report their status.
        """
        for worker in self.__workers:
            try:
                yield worker.callRemote("reportStatus")
            except Exception as ex:
                self.__log.error(
                    "Failed to report status (%s): %s",
                    worker.workerId, ex
                )

    def submit(self, job):
        """Submits a job for execution.

        Returns a deferred that will fire when execution completes.
        """
        ajob = AsyncServiceCallJob(job)
        self.__worklist.push(ajob)
        self.__reactor.callLater(0, self.__execute)
        return ajob.deferred

    @defer.inlineCallbacks
    def __execute(self):
        """Executes the next job in the worklist.
        """
        # An empty worklist means nothing to do.
        if not self.__worklist:
            defer.returnValue(None)

        # Find a worker that's not busy.
        worker = next((
            worker for worker in self.__workers if not worker.busy
        ), None)
        if worker is None:
            # If all workers are busy, try again later.
            self.__reactor.callLater(0.1, self.__execute)
            defer.returnValue(None)

        asyncjob = self.__worklist.pop()
        job = asyncjob.job

        try:
            self.__beginWork(worker, job)

            serviceKey = (job.service, job.monitor)
            service = self.__workers[worker].get(serviceKey)
            if service is None:
                try:
                    service = yield worker.callRemote(
                        "getService", job.service, job.monitor
                    )
                except Exception as ex:
                    self.__log.exception(
                        "Failed to getService: %s", job.service
                    )
                    asyncjob.deferred.errback(
                        pb.Error("Internal ZenHub error: %s" % str(ex))
                    )
                    defer.returnValue(None)
                else:
                    self.__workers[worker][serviceKey] = service

            try:
                result = yield service.callRemote(
                    job.method, *job.args, **job.kwargs
                )
            except (RemoteException, pb.RemoteError) as ex:
                asyncjob.deferred.errback(ex)
            except Exception as ex:
                self.__log.exception(
                    "Failure on %s.%s", job.service, job.method
                )
                asyncjob.deferred.errback(
                    pb.Error("Internal ZenHub error: %s" % (ex,))
                )
            else:
                asyncjob.deferred.callback(result)

        finally:
            self.__endWork(worker, job)

    def __beginWork(self, worker, job):
        now = time.time()
        jobDesc = "%s:%s.%s" % (job.monitor, job.service, job.method)
        stats = self.__workTracker.pop(worker.workerId, None)
        idletime = now - stats.lastupdate if stats else 0

        execStats = self.__executionTimer[job.method]
        execStats.count += 1
        execStats.idle_total += idletime
        execStats.last_called_time = now

        self.__log.debug(
            "Giving %s to worker %d, (%s)",
            job.method, worker.workerId, jobDesc
        )
        self.__workTracker[worker.workerId] = \
            WorkerStats("Busy", jobDesc, now, idletime)

        for listener in self.__executeListeners:
            listener()

        worker.busy = True

    def __endWork(self, worker, job):
        now = time.time()
        self.__executionTimer[job.method].last_called_time = now
        stats = self.__workTracker.pop(worker.workerId, None)
        if stats:
            elapsed = now - stats.lastupdate
            self.__executionTimer[job.method].running_total += elapsed
            self.__log.debug(
                "Worker %s, work %s finished in %s",
                worker.workerId, stats.description, elapsed
            )
            self.__workTracker[worker.workerId] = \
                WorkerStats("Idle", stats.description, now, 0)
        worker.busy = False


WorkerStats = collections.namedtuple(
    "WorkerStats",
    "status description lastupdate previdle"
)


class ExecutionStats(object):

    __slots__ = ("count", "idle_total", "running_total", "last_called_time")

    def __init__(self):
        self.count = 0
        self.idle_total = 0.0
        self.running_total = 0.0
        self.last_called_time = 0


ServiceCallJob = collections.namedtuple(
    "ServiceCallJob", "service monitor method args kwargs"
)


class AsyncServiceCallJob(object):
    """Wraps a ServiceCallJob object to track for use with
    WorkerPoolDispatcher.
    """

    def __init__(self, job):
        self.job = job
        self.method = job.method
        self.deferred = defer.Deferred()
        self.recvtime = time.time()
