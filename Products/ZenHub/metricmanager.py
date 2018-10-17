##############################################################################
#
# Copyright (C) Zenoss, Inc. 2018, all rights reserved.
#
# This content is made available according to terms specified in
# License.zenoss under the directory where your Zenoss product is installed.
#
##############################################################################

import os

import Globals  # required to import zenoss Products

from Products.ZenUtils.Utils import unused
from Products.ZenUtils.MetricReporter import TwistedMetricReporter
from Products.ZenUtils.DaemonStats import DaemonStats
from Products.ZenUtils.metricwriter import (
    ThresholdNotifier,
    DerivativeTracker,
    MetricWriter,
    FilteredMetricWriter,
    AggregateMetricWriter,
)

from Products.ZenModel.BuiltInDS import BuiltInDS

from Products.ZenHub.metricpublisher.publisher import (
    RedisListPublisher,
    HttpPostPublisher
)

unused(Globals)


class MetricManager(object):
    '''General interface for storing and reporting metrics
    metric publisher: publishes metrics to an external system (redis, http)
    metric writer: drives metric pulisher(s), calling their .put method
    metric reporter: once its .start method is called,
        periodically calls writer.write_metric, to publish stored metrics
    '''

    def __init__(self, daemon_tags):
        self.daemon_tags = daemon_tags
        self._metric_writer = None
        self._metric_reporter = None

    def start(self):
        self.metricreporter.start()

    def stop(self):
        self.metricreporter.stop()

    @property
    def metricreporter(self):
        if not self._metric_reporter:
            self._metric_reporter = TwistedMetricReporter(
                metricWriter=self.metric_writer, tags=self.daemon_tags
            )

        return self._metric_reporter

    @property
    def metric_writer(self):
        if not self._metric_writer:
            self._metric_writer = MetricWriter(RedisListPublisher())
            self._metric_writer = self._setup_cc_metric_writer(
                self._metric_writer
            )

        return self._metric_writer

    def _setup_cc_metric_writer(self, metric_writer):
        cc = os.environ.get("CONTROLPLANE", "0") == "1"
        internal_url = os.environ.get("CONTROLPLANE_CONSUMER_URL", None)
        if cc and internal_url:
            username = os.environ.get("CONTROLPLANE_CONSUMER_USERNAME", "")
            password = os.environ.get("CONTROLPLANE_CONSUMER_PASSWORD", "")
            _publisher = HttpPostPublisher(username, password, internal_url)
            internal_metric_writer = FilteredMetricWriter(
                _publisher, self._internal_metric_filter
            )
            metric_writer = AggregateMetricWriter(
                [metric_writer, internal_metric_writer]
            )
        return metric_writer

    @staticmethod
    def _internal_metric_filter(metric, value, timestamp, tags):
        return tags and tags.get("internal", False)

    def get_rrd_stats(self, hub_config, send_event):
        rrd_stats = DaemonStats()
        thresholds = hub_config.getThresholdInstances(BuiltInDS.sourcetype)
        threshold_notifier = ThresholdNotifier(send_event, thresholds)
        derivative_tracker = DerivativeTracker()

        rrd_stats.config(
            'zenhub',
            hub_config.id,
            self.metric_writer,
            threshold_notifier,
            derivative_tracker
        )

        return rrd_stats
