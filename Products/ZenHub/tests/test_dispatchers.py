##############################################################################
#
# Copyright (C) Zenoss, Inc. 2018, all rights reserved.
#
# This content is made available according to terms specified in
# License.zenoss under the directory where your Zenoss product is installed.
#
##############################################################################

import time

from unittest import TestCase
from mock import Mock, patch, call, sentinel

from Products.ZenHub.dispatchers import (
    DispatchingExecutor, EventDispatcher, WorkerPoolDispatcher,
    ServiceCallJob, AsyncServiceCallJob,
    RemoteException, pb, defer
)

PATH = {'src': 'Products.ZenHub.dispatchers'}


class Callback(object):
    """Helper class for capturing the results of Twisted Deferred objects.

    Callback objects can be used for both successful and errored results.

    The result is stored in the 'result' attribute of the Callback object.

    E.g.
        cb = Callback()
        dfr = obj.returns_a_deferred()
        dfr.addCallback(cb)

        self.assertTrue(cb.result)
    """

    def __init__(self):
        self.result = None

    def __call__(self, r):
        self.result = r


class DispatchingExecutorTest(TestCase):

    def test_no_args_to_init(self):
        executor = DispatchingExecutor()
        self.assertEqual(None, executor.default)
        self.assertEqual((), executor.dispatchers)

    def test_only_default(self):
        default = Mock()
        executor = DispatchingExecutor(default=default)
        self.assertEqual(default, executor.default)
        self.assertEqual((), executor.dispatchers)

    def test_dispatcher_registration(self):
        executor = DispatchingExecutor()
        dispatcher = Mock()
        dispatcher.routes = (("service", "method"),)

        executor.register(dispatcher)

        self.assertEqual((dispatcher,), executor.dispatchers)

        job = Mock()
        job.service = "service"
        job.method = "method"

        dfr = executor.submit(job)

        self.assertIsInstance(dfr, defer.Deferred)
        dispatcher.submit.assert_called_once_with(job)

    def test_executor_init(self):
        dispatcher = Mock()
        dispatcher.routes = (("service", "method"),)
        executor = DispatchingExecutor([dispatcher])

        self.assertEqual((dispatcher,), executor.dispatchers)

        job = Mock()
        job.service = "service"
        job.method = "method"

        dfr = executor.submit(job)

        self.assertIsInstance(dfr, defer.Deferred)
        dispatcher.submit.assert_called_once_with(job)

    def test_executor_init_with_default(self):
        dispatcher = Mock()
        dispatcher.routes = (("service", "method"),)
        default = Mock()
        executor = DispatchingExecutor([dispatcher], default=default)

        self.assertEqual((dispatcher,), executor.dispatchers)
        self.assertEqual(default, executor.default)

        job = Mock()
        job.service = "other_service"
        job.method = "method"

        dfr = executor.submit(job)

        self.assertIsInstance(dfr, defer.Deferred)
        default.submit.assert_called_once_with(job)
        dispatcher.submit.assert_not_called()

    def test_submit_bad_job(self):
        dispatcher = Mock()
        dispatcher.routes = (("service", "method"),)
        executor = DispatchingExecutor([dispatcher])

        self.assertEqual((dispatcher,), executor.dispatchers)

        job = Mock()
        job.service = "other_service"
        job.method = "method"

        handler = Callback()
        dfr = executor.submit(job)
        dfr.addErrback(handler)

        while not dfr.called:
            time.sleep(0.01)

        failure = handler.result

        with self.assertRaisesRegexp(RuntimeError, ".*other_service.*"):
            raise failure.value

        dispatcher.submit.assert_not_called()

    def test_bad_routes(self):
        executor = DispatchingExecutor()

        dispatcher1 = Mock()
        dispatcher1.routes = (("service", "method"),)

        executor.register(dispatcher1)

        dispatcher2 = Mock()
        dispatcher2.routes = (("service", "method"),)

        with self.assertRaisesRegexp(
                ValueError, ".*('service', 'method').*"):
            executor.register(dispatcher2)

        self.assertEqual((dispatcher1,), executor.dispatchers)


class EventDispatcherTest(TestCase):

    def setUp(self):
        self.em = Mock()
        self.dispatcher = EventDispatcher(self.em)

    def test_routes(self):
        routes = (
            ("EventService", "sendEvent"), ("EventService", "sendEvents")
        )
        self.assertSequenceEqual(routes, EventDispatcher.routes)

    def test_sendEvent_job(self):
        job = Mock()
        job.service = "EventService"
        job.method = "sendEvent"
        job.args = ["event"]
        job.kwargs = {}

        dfr = self.dispatcher.submit(job)

        self.assertIsInstance(dfr, defer.Deferred)
        self.assertEqual(self.em.sendEvent.return_value, dfr.result)
        self.em.sendEvent.assert_called_once_with("event")

    def test_sendEvents_job(self):
        job = Mock()
        job.service = "EventService"
        job.method = "sendEvents"
        job.args = [["event"]]
        job.kwargs = {}

        dfr = self.dispatcher.submit(job)

        self.assertIsInstance(dfr, defer.Deferred)
        self.assertEqual(self.em.sendEvents.return_value, dfr.result)
        self.em.sendEvents.assert_called_once_with(["event"])

    @patch("{src}.getattr".format(**PATH))
    def test_bad_job(self, mock_getattr):
        job = Mock()
        job.service = "EventService"
        job.method = "badmethod"
        job.args = [["event"]]
        job.kwargs = {}

        mock_getattr.return_value = None

        handler = Callback()
        dfr = self.dispatcher.submit(job)
        dfr.addErrback(handler)

        with self.assertRaisesRegexp(AttributeError, ".*'badmethod'.*"):
            raise handler.result.value

        self.em.sendEvent.assert_not_called()
        self.em.sendEvents.assert_not_called()

    def test_exception_from_service(self):
        job = Mock()
        job.service = "EventService"
        job.method = "sendEvent"
        job.args = []
        job.kwargs = {}

        error = ValueError("boom")
        self.em.sendEvent.side_effect = error

        handler = Callback()
        dfr = self.dispatcher.submit(job)
        dfr.addErrback(handler)

        with self.assertRaisesRegexp(ValueError, "boom"):
            raise handler.result.value

        self.em.sendEvent.assert_called_once_with()


class WorkerPoolDispatcherTest(TestCase):

    def setUp(self):
        self.logging_patcher = patch(
            "{src}.logging".format(**PATH), autospec=True
        )
        self.logger = self.logging_patcher.start()
        self.addCleanup(self.logging_patcher.stop)

        self.reactor = Mock()
        self.worklist = Mock()
        self.worklist_len = Mock()
        self.worklist_len.return_value = True
        setattr(self.worklist, "__len__", self.worklist_len)

        self.dispatcher = WorkerPoolDispatcher(self.reactor, self.worklist)

    def test_routes(self):
        self.assertEqual((), self.dispatcher.routes)

    def test_worker_management(self):
        worker = Mock()
        self.dispatcher.add(worker)
        self.assertTrue(worker in self.dispatcher)
        self.dispatcher.remove(worker)
        self.assertFalse(worker in self.dispatcher)

    @patch("{src}.AsyncServiceCallJob".format(**PATH))
    def test_submit(self, asyncjob):
        job = Mock()
        ajob = asyncjob.return_value
        expected_dfr = ajob.deferred
        dfr = self.dispatcher.submit(job)

        self.assertIs(expected_dfr, dfr)
        asyncjob.assert_called_once_with(job)
        self.worklist.push.assert_called_once_with(ajob)
        self.reactor.callLater.assert_called_once_with(
            0, self.dispatcher._WorkerPoolDispatcher__execute
        )

    def test___execute_no_jobs(self):
        self.worklist_len.return_value = False
        dfr = self.dispatcher._WorkerPoolDispatcher__execute()
        self.assertIsNone(dfr.result)
        self.worklist_len.assert_called_once_with()

    def test___execute_no_workers(self):
        dfr = self.dispatcher._WorkerPoolDispatcher__execute()
        self.assertIsNone(dfr.result)
        self.reactor.callLater.assert_called_once_with(
            0.1, self.dispatcher._WorkerPoolDispatcher__execute
        )

    def test___execute_getService_failure(self):
        ajob = Mock(
            name="asyncjob", spec_set=["job", "deferred"],
            job=ServiceCallJob("service", "localhost", "method", [], {}),
            deferred=defer.Deferred()
        )
        self.worklist.pop.return_value = ajob

        job_cb = Callback()
        ajob.deferred.addErrback(job_cb)

        worker = Mock(name="worker", busy=False)
        expected_failure = RuntimeError("boom")
        worker.callRemote.side_effect = expected_failure

        self.dispatcher.add(worker)

        exec_cb = Callback()
        dfr = self.dispatcher._WorkerPoolDispatcher__execute()
        dfr.addErrback(exec_cb)

        self.assertIsNone(exec_cb.result)
        worker.callRemote.assert_called_once_with(
            "getService", ajob.job.service, ajob.job.monitor
        )
        self.assertIsInstance(job_cb.result.value, pb.Error)
        self.assertIsNone(dfr.result)
        self.logger.getLogger.return_value.exception.assert_called_once_with(
            "Failed to getService: %s", ajob.job.service
        )
        self.assertFalse(worker.busy)

    def test___execute_RemoteException(self):
        ajob = Mock(
            name="asyncjob", spec_set=["job", "deferred"],
            job=ServiceCallJob("service", "localhost", "method", [], {}),
            deferred=defer.Deferred()
        )
        self.worklist.pop.return_value = ajob

        job_cb = Callback()
        ajob.deferred.addErrback(job_cb)

        worker = Mock(name="worker", busy=False)
        service = worker.callRemote.return_value

        mesg = "boom"
        tb = sentinel.tb
        expected_failure = RemoteException(mesg, tb)
        service.callRemote.side_effect = expected_failure

        self.dispatcher.add(worker)

        exec_cb = Callback()
        dfr = self.dispatcher._WorkerPoolDispatcher__execute()
        dfr.addErrback(exec_cb)

        self.assertIsNone(exec_cb.result)
        service.callRemote.assert_called_once_with(ajob.job.method)
        self.assertEqual(expected_failure, job_cb.result.value)
        self.assertIsNone(dfr.result)
        self.logger.getLogger.return_value.exception.assert_not_called()
        self.assertFalse(worker.busy)

    def test___execute_pbRemoteError(self):
        ajob = Mock(
            name="asyncjob", spec_set=["job", "deferred"],
            job=ServiceCallJob("service", "localhost", "method", [], {}),
            deferred=defer.Deferred()
        )
        self.worklist.pop.return_value = ajob

        job_cb = Callback()
        ajob.deferred.addErrback(job_cb)

        worker = Mock(name="worker", busy=False)
        service = worker.callRemote.return_value

        expected_failure = pb.RemoteError("type", "boom", "tb")
        service.callRemote.side_effect = expected_failure

        self.dispatcher.add(worker)

        exec_cb = Callback()
        dfr = self.dispatcher._WorkerPoolDispatcher__execute()
        dfr.addErrback(exec_cb)

        self.assertIsNone(exec_cb.result)
        service.callRemote.assert_called_once_with(ajob.job.method)
        self.assertEqual(expected_failure, job_cb.result.value)
        self.assertIsNone(dfr.result)
        self.logger.getLogger.return_value.exception.assert_not_called()
        self.assertFalse(worker.busy)

    def test___execute_callRemote_failure(self):
        ajob = Mock(
            name="asyncjob", spec_set=["job", "deferred"],
            job=ServiceCallJob("service", "localhost", "method", [], {}),
            deferred=defer.Deferred()
        )
        self.worklist.pop.return_value = ajob

        job_cb = Callback()
        ajob.deferred.addErrback(job_cb)

        worker = Mock(name="worker", busy=False)
        service = worker.callRemote.return_value

        expected_failure = RuntimeError("boom")
        service.callRemote.side_effect = expected_failure

        self.dispatcher.add(worker)

        exec_cb = Callback()
        dfr = self.dispatcher._WorkerPoolDispatcher__execute()
        dfr.addErrback(exec_cb)

        self.assertIsNone(exec_cb.result)
        service.callRemote.assert_called_once_with(ajob.job.method)
        self.assertIsInstance(job_cb.result.value, pb.Error)
        self.assertIsNone(dfr.result)
        self.logger.getLogger.return_value.exception.assert_called_once_with(
            "Failure on %s.%s", ajob.job.service, ajob.job.method
        )
        self.assertFalse(worker.busy)

    def test___execute_success(self):
        ajob = Mock(
            name="asyncjob", spec_set=["job", "deferred"],
            job=ServiceCallJob("service", "localhost", "method", [], {}),
            deferred=defer.Deferred()
        )
        self.worklist.pop.return_value = ajob

        worker = Mock(name="worker", busy=False)
        service = worker.callRemote.return_value
        expected_result = service.callRemote.return_value

        self.dispatcher.add(worker)

        exec_cb = Callback()
        dfr = self.dispatcher._WorkerPoolDispatcher__execute()
        dfr.addErrback(exec_cb)

        self.assertIsNone(exec_cb.result)
        service.callRemote.assert_called_once_with(ajob.job.method)
        self.assertEqual(expected_result, ajob.deferred.result)
        self.assertIsNone(dfr.result)
        self.logger.getLogger.return_value.exception.assert_not_called()
        self.assertFalse(worker.busy)

    def test_cached_services(self):
        job = ServiceCallJob("service", "localhost", "method", [], {})

        ajob1 = Mock(name="ajob1", job=job, deferred=defer.Deferred())
        ajob2 = Mock(name="ajob2", job=job, deferred=defer.Deferred())

        worker = Mock(name="worker", busy=False)
        service = worker.callRemote.return_value
        expected_result = service.callRemote.return_value

        self.dispatcher.add(worker)

        self.worklist.pop.return_value = ajob1
        self.dispatcher._WorkerPoolDispatcher__execute()

        self.worklist.pop.return_value = ajob2
        self.dispatcher._WorkerPoolDispatcher__execute()

        worker.callRemote.assert_called_once_with(
            "getService", job.service, job.monitor
        )
        service.callRemote.assert_has_calls([
            call(job.method), call(job.method)
        ])
        self.assertEqual(expected_result, ajob1.deferred.result)
        self.assertEqual(expected_result, ajob2.deferred.result)

    @patch("{src}.get_worklist_metrics".format(**PATH), autospec=True)
    def test_getStats(self, get_worklist_metrics):
        g, w, t = self.dispatcher.getStats()

        get_worklist_metrics.assert_called_once_with(self.worklist)
        self.assertEqual(get_worklist_metrics.return_value, g)
        self.assertIsInstance(w, dict)
        self.assertIsInstance(t, dict)


class ServiceCallJobTest(TestCase):

    def test___init__(self):
        name = "name"
        monitor = "monitor"
        method = "method"
        args = tuple()
        kw = {}
        job = ServiceCallJob(name, monitor, method, args, kw)
        self.assertEqual(job.service, name)
        self.assertEqual(job.monitor, monitor)
        self.assertEqual(job.method, method)
        self.assertEqual(job.args, args)
        self.assertEqual(job.kwargs, kw)


class AsyncServiceCallJobTest(TestCase):

    def test___init__(self):
        job = ServiceCallJob("name", "monitor", "method", [], {})
        ajob = AsyncServiceCallJob(job)
        self.assertEqual(job, ajob.job)
        self.assertEqual(job.method, ajob.method)
        self.assertIsInstance(ajob.deferred, defer.Deferred)
        self.assertIsInstance(ajob.recvtime, float)
