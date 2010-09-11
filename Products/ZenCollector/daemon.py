###########################################################################
#
# This program is part of Zenoss Core, an open source monitoring platform.
# Copyright (C) 2009, 2010 Zenoss Inc.
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 2 as published by
# the Free Software Foundation.
#
# For complete information please visit: http://www.zenoss.com/oss/
#
###########################################################################

import signal
import time
import logging

import zope.interface

from twisted.internet import defer, reactor
from twisted.python.failure import Failure

from Products.ZenCollector.interfaces import ICollector,\
                                             ICollectorPreferences,\
                                             IDataService,\
                                             IEventService,\
                                             IFrameworkFactory,\
                                             ITaskSplitter,\
                                             IConfigurationListener,\
                                             IStatistic,\
                                             IStatisticsService
from Products.ZenHub.PBDaemon import PBDaemon, FakeRemote
from Products.ZenRRD.RRDDaemon import RRDDaemon
from Products.ZenRRD.RRDUtil import RRDUtil
from Products.ZenRRD.Thresholds import Thresholds
from Products.ZenUtils.Utils import importClass, readable_time

log = logging.getLogger("zen.daemon")

class DummyListener(object):
    zope.interface.implements(IConfigurationListener)
    
    def deleted(self, configurationId):
        """
        Called when a configuration is deleted from the collector
        """
        log.debug('DummyListener: configuration %s deleted' % configurationId)

    def added(self, configuration):
        """
        Called when a configuration is added to the collector
        """
        log.debug('DummyListener: configuration %s added' % configuration)


    def updated(self, newConfiguration):
        """
        Called when a configuration is updated in collector
        """
        log.debug('DummyListener: configuration %s updated' % newConfiguration)

DUMMY_LISTENER = DummyListener()
CONFIG_LOADER_NAME = 'configLoader'

class CollectorDaemon(RRDDaemon):
    """
    The daemon class for the entire ZenCollector framework. This class bridges
    the gap between the older daemon framework and ZenCollector. New collectors
    no longer should extend this class to implement a new collector.
    """
    zope.interface.implements(ICollector,
                              IDataService,
                              IEventService)

    def __init__(self, preferences, taskSplitter, 
                 configurationLister=DUMMY_LISTENER,
                 initializationCallback=None,
                 stoppingCallback=None):
        """
        Constructs a new instance of the CollectorDaemon framework. Normally
        only a singleton instance of a CollectorDaemon should exist within a
        process, but this is not enforced.
        
        @param preferences: the collector configuration
        @type preferences: ICollectorPreferences
        @param taskSplitter: the task splitter to use for this collector
        @type taskSplitter: ITaskSplitter
        @param initializationCallback: a callable that will be executed after
                                       connection to the hub but before
                                       retrieving configuration information
        @type initializationCallback: any callable
        @param stoppingCallback: a callable that will be executed first during
                                 the stopping process. Exceptions will be
                                 logged but otherwise ignored.
        @type stoppingCallback: any callable
        """
        # create the configuration first, so we have the collector name
        # available before activating the rest of the Daemon class hierarchy.
        if not ICollectorPreferences.providedBy(preferences):
            raise TypeError("configuration must provide ICollectorPreferences")
        else:
            self._prefs = preferences

        if not ITaskSplitter.providedBy(taskSplitter):
            raise TypeError("taskSplitter must provide ITaskSplitter")
        else:
            self._taskSplitter = taskSplitter
        
        if not IConfigurationListener.providedBy(configurationLister):
            raise TypeError(
                    "configurationLister must provide IConfigurationListener")
        self._configListener = configurationLister
        self._initializationCallback = initializationCallback
        self._stoppingCallback = stoppingCallback

        # register the various interfaces we provide the rest of the system so
        # that collector implementors can easily retrieve a reference back here
        # if needed
        zope.component.provideUtility(self, ICollector)
        zope.component.provideUtility(self, IEventService)
        zope.component.provideUtility(self, IDataService)

        # setup daemon statistics
        self._statService = StatisticsService()
        self._statService.addStatistic("devices", "GAUGE")
        self._statService.addStatistic("cyclePoints", "GAUGE")
        self._statService.addStatistic("dataPoints", "DERIVE")
        zope.component.provideUtility(self._statService, IStatisticsService)

        # register the collector's own preferences object so it may be easily
        # retrieved by factories, tasks, etc.
        zope.component.provideUtility(self._prefs,
                                      ICollectorPreferences,
                                      self._prefs.collectorName)

        super(CollectorDaemon, self).__init__(name=self._prefs.collectorName)

        self._devices = set()
        self._thresholds = Thresholds()
        self._unresponsiveDevices = set()
        self._rrd = None
        self.reconfigureTimeout = None

        # keep track of pending tasks if we're doing a single run, and not a
        # continuous cycle
        if not self.options.cycle:
            self._completedTasks = 0
            self._pendingTasks = []

        frameworkFactory = zope.component.queryUtility(IFrameworkFactory)
        self._configProxy = frameworkFactory.getConfigurationProxy()
        self._scheduler = frameworkFactory.getScheduler()
        self._scheduler.maxTasks = self.options.maxTasks
        self._ConfigurationLoaderTask = frameworkFactory.getConfigurationLoaderTask()
        
        # OLD - set the initialServices attribute so that the PBDaemon class
        # will load all of the remote services we need.
        self.initialServices = PBDaemon.initialServices +\
            [self._prefs.configurationService]

        # trap SIGUSR2 so that we can display detailed statistics
        signal.signal(signal.SIGUSR2, self._signalHandler)

        # let the configuration do any additional startup it might need
        self._prefs.postStartup()

    def buildOptions(self):
        """
        Method called by CmdBase.__init__ to build all of the possible 
        command-line options for this collector daemon.
        """
        super(CollectorDaemon, self).buildOptions()

        maxTasks = getattr(self._prefs, 'maxTasks', None)
        defaultMax = maxTasks if maxTasks else 'Unlimited' 
        
        self.parser.add_option('--maxparallel',
                                dest='maxTasks',
                                type='int',
                                default= maxTasks,
                                help='Max number of tasks to run at once, default %default')

        frameworkFactory = zope.component.queryUtility(IFrameworkFactory)
        self._frameworkBuildOptions = frameworkFactory.getFrameworkBuildOptions()
        if self._frameworkBuildOptions:
            self._frameworkBuildOptions(self.parser)

        # give the collector configuration a chance to add options, too
        self._prefs.buildOptions(self.parser)

    def parseOptions(self):
        super(CollectorDaemon, self).parseOptions()
        self._prefs.options = self.options

    def connected(self):
        """
        Method called by PBDaemon after a connection to ZenHub is established.
        """
        return self._startup()

    def _getInitializationCallback(self):
        def doNothing():
            pass

        if self._initializationCallback is not None:
            return self._initializationCallback
        else:
            return doNothing

    def connectTimeout(self):
        super(CollectorDaemon, self).connectTimeout()
        return self._startup()

    def _startup(self):
        d = defer.maybeDeferred( self._getInitializationCallback() )
        d.addCallback( self._startConfigCycle )
        d.addCallback( self._maintenanceCycle )
        d.addErrback( self._errorStop )
        return d

    def watchdogCycleTime(self):
        """
        Return our cycle time (in minutes)

        @return: cycle time
        @rtype: integer
        """
        return self._prefs.cycleInterval * 2

    def getRemoteConfigServiceProxy(self):
        """
        Called to retrieve the remote configuration service proxy object.
        """
        return self.services.get(self._prefs.configurationService,
                                 FakeRemote())

    def writeRRD(self, path, value, rrdType, rrdCommand=None, cycleTime=None,
                 min='U', max='U', threshEventData=None):
        now = time.time()

        # save the raw data directly to the RRD files
        value = self._rrd.save(path,
                               value,
                               rrdType,
                               rrdCommand,
                               cycleTime,
                               min,
                               max)

        # check for threshold breaches and send events when needed
        for ev in self._thresholds.check(path, now, value):
            ev['eventKey'] = path.rsplit('/')[-1]
            if threshEventData:
                ev.update(threshEventData)
            self.sendEvent(ev)

    def stop(self, ignored=""):
        if self._stoppingCallback is not None:
            try:
                self._stoppingCallback()
            except Exception, e:
                self.log.exception('Exception while stopping daemon')
        super(CollectorDaemon, self).stop( ignored )

    def remote_deleteDevice(self, devId):
        """
        Called remotely by ZenHub when a device we're monitoring is deleted.
        """
        self._deleteDevice(devId)

    def remote_updateDeviceConfig(self, config):
        """
        Called remotely by ZenHub when asynchronous configuration updates occur.
        """
        self.log.debug("Device %s updated", config.id)

        if not self.options.device or self.options.device == config.id:
            self._updateConfig(config)
            self._configProxy.updateConfigProxy(self._prefs, config)
            
    def remote_notifyConfigChanged(self):
        """
        Called from zenhub to notify that the entire config should be updated  
        """
        self.log.debug('zenhub is telling us to update the entire config')
        if self.reconfigureTimeout and not self.reconfigureTimeout.called:
            self.reconfigureTimeout.cancel()
        d = defer.maybeDeferred(self._rebuildConfig)
        self.reconfigureTimeout = reactor.callLater(30, d, False)

    def _rebuildConfig(self):
        """
        Delete and re-add the configuration tasks to completely re-build the configuration.
        """
        self._scheduler.removeTasksForConfig(CONFIG_LOADER_NAME)
        self._startConfigCycle()

    def _taskCompleteCallback(self, taskName):
        # if we're not running a normal daemon cycle then we need to shutdown
        # once all of our pending tasks have completed
        if not self.options.cycle:
            try:
                self._pendingTasks.remove(taskName)
            except ValueError:
                pass

            self._completedTasks += 1

            # if all pending tasks have been completed then shutdown the daemon
            if len(self._pendingTasks) == 0:
                self._displayStatistics()
                self.stop()

    def _updateConfig(self, cfg):
        configId = cfg.id
        self.log.debug("Processing configuration for %s", configId)

        if configId in self._devices:
            self._scheduler.removeTasksForConfig(configId)
            self._configListener.updated(cfg)
        else:
            self._devices.add(configId)
            self._configListener.added(cfg)

        newTasks = self._taskSplitter.splitConfiguration([cfg])
        self.log.debug("Tasks for config %s: %s", configId, newTasks)

        for (taskName, task) in newTasks.iteritems():
            #if not cycling run the task immediately otherwise let the scheduler
            #decide when to run the task
            now = not self.options.cycle
            self._scheduler.addTask(task, self._taskCompleteCallback, now)

            # TODO: another hack?
            if hasattr(cfg, 'thresholds'):
                self._thresholds.updateForDevice(configId, cfg.thresholds)

            # if we're not running a normal daemon cycle then keep track of the
            # tasks we just added for this device so that we can shutdown once
            # all pending tasks have completed
            if not self.options.cycle:
                self._pendingTasks.append(taskName)

    def _updateDeviceConfigs(self, updatedConfigs):
        """
        Update the device configurations for the devices managed by this
        collector.
        @param deviceConfigs a list of device configurations
        @type deviceConfigs list of name,value tuples
        """
        self.log.debug("updateDeviceConfigs: updatedConfigs=%s", (map(str, updatedConfigs)))

        deleted = self._devices.copy()

        for cfg in updatedConfigs:
            deleted.discard(cfg.id)
            self._updateConfig(cfg)

        # remove tasks for the deleted devices
        for configId in deleted:
            self._deleteDevice(configId)
            
    def _deleteDevice(self, deviceId):
        self.log.debug("Device %s deleted" % deviceId)

        self._devices.discard(deviceId)
        self._configListener.deleted(deviceId)
        self._configProxy.deleteConfigProxy(self._prefs, deviceId)
        self._scheduler.removeTasksForConfig(deviceId)

    def _errorStop(self, result):
        """
        Twisted callback to receive fatal messages.
        
        @param result: the Twisted failure
        @type result: failure object
        """
        if isinstance(result, Failure):
            msg = result.getErrorMessage()
        else:
            msg = str(result)
        self.log.critical("Unrecoverable Error: %s", msg)
        self.stop()

    def _startConfigCycle(self, result=None):
        configLoader = self._ConfigurationLoaderTask(CONFIG_LOADER_NAME,
                                               taskConfig=self._prefs,
                                               daemonRef=self)
        # Run initial maintenance cycle as soon as possible
        # TODO: should we not run maintenance if running in non-cycle mode?
        self._scheduler.addTask(configLoader, now=True)
        return defer.succeed(None)

    def _setCollectorPreferences(self, preferenceItems):
        for name, value in preferenceItems.iteritems():
            if not hasattr(self._prefs, name):
                # TODO: make a super-low level debug mode?  The following message isn't helpful
                #self.log.debug("Preferences object does not have attribute %s",
                #               name)
                setattr(self._prefs, name, value)
            elif getattr(self._prefs, name) != value:
                self.log.debug("Updated %s preference to %s", name, value)
                setattr(self._prefs, name, value)

    def _loadThresholdClasses(self, thresholdClasses):
        self.log.debug("Loading classes %s", thresholdClasses)
        for c in thresholdClasses:
            try:
                importClass(c)
            except ImportError:
                log.exception("Unable to import class %s", c)

    def _configureRRD(self, rrdCreateCommand, thresholds):
        self._rrd = RRDUtil(rrdCreateCommand, self._prefs.cycleInterval)
        self.rrdStats.config(self.options.monitor,
                             self.name,
                             thresholds,
                             rrdCreateCommand)

    def _isRRDConfigured(self):
        return (self.rrdStats and self._rrd)

    def _maintenanceCycle(self, ignored=None):
        """
        Perform daemon maintenance processing on a periodic schedule. Initially
        called after the daemon configuration loader task is added, but afterward
        will self-schedule each run.
        """
        self.log.debug("Performing periodic maintenance")
        interval = self._prefs.cycleInterval

        def _processDeviceIssues(result):
            self.log.debug("deviceIssues=%r", result)

            # Device ping issues returns as a tuple of (deviceId, count, total)
            # and we just want the device id
            newUnresponsiveDevices = set([i[0] for i in result])

            clearedDevices = self._unresponsiveDevices.difference(newUnresponsiveDevices)
            for devId in clearedDevices:
                self.log.debug("Resuming tasks for device %s", devId)
                self._scheduler.resumeTasksForConfig(devId)

            self._unresponsiveDevices = newUnresponsiveDevices
            for devId in self._unresponsiveDevices:
                self.log.debug("Pausing tasks for device %s", devId)
                self._scheduler.pauseTasksForConfig(devId)

            return result

        def _getDeviceIssues(result):
            # TODO: handle different types of device issues, such as WMI issues
            d = self.getDevicePingIssues()
            return d

        def _postStatistics(result):
            self._displayStatistics()

            # update and post statistics if we've been configured to do so
            if self._isRRDConfigured():
                stat = self._statService.getStatistic("devices")
                stat.value = len(self._devices)

                stat = self._statService.getStatistic("cyclePoints")
                stat.value = self._rrd.endCycle()

                stat = self._statService.getStatistic("dataPoints")
                stat.value = self._rrd.dataPoints

                events = self._statService.postStatistics(self.rrdStats,
                                                          interval)
                self.sendEvents(events)

            return result

        def _maintenance():
            if self.options.cycle:
                d = defer.maybeDeferred(self.heartbeat)
                d.addCallback(_postStatistics)
                d.addCallback(_getDeviceIssues)
                d.addCallback(_processDeviceIssues)
            else:
                d = defer.succeed(None)
            return d

        def _reschedule(result):
            if isinstance(result, Failure):
                self.log.error("Maintenance failed: %s",
                               result.getErrorMessage())

            if interval > 0:
                self.log.debug("Rescheduling maintenance in %ds", interval)
                reactor.callLater(interval, self._maintenanceCycle)

        d = _maintenance()
        d.addBoth(_reschedule)
        return d

    def _displayStatistics(self, verbose=False):
        if self._rrd:
            self.log.info("%d devices processed (%d datapoints)",
                          len(self._devices), self._rrd.dataPoints)
        else:
            self.log.info("%d devices processed (0 datapoints)",
                          len(self._devices))

        self._scheduler.displayStatistics(verbose)

    def _signalHandler(self, signum, frame):
        self._displayStatistics(True)


class Statistic(object):
    zope.interface.implements(IStatistic)

    def __init__(self, name, type):
        self.value = 0
        self.name = name
        self.type = type


class StatisticsService(object):
    zope.interface.implements(IStatisticsService)

    def __init__(self):
        self._stats = {}

    def addStatistic(self, name, type):
        if self._stats.has_key(name):
            raise NameError("Statistic %s already exists" % name)

        if type not in ('DERIVE', 'COUNTER', 'GAUGE'):
            raise TypeError("Statistic type %s not supported" % type)

        stat = Statistic(name, type)
        self._stats[name] = stat

    def getStatistic(self, name):
        return self._stats[name]

    def postStatistics(self, rrdStats, interval):
        events = []

        for stat in self._stats.values():
            # figure out which function to use to post this statistical data
            if stat.type is 'COUNTER':
                func = rrdStats.counter
            elif stat.type is 'GAUGE':
                func = rrdStats.gauge
            elif stat.type is 'DERIVE':
                func = rrdStats.derive
            else:
                raise TypeError("Statistic type %s not supported" % stat.type)

            events += func(stat.name, interval, stat.value)

            # counter is an ever-increasing value, but otherwise...
            if stat.type is not 'COUNTER':
                stat.value = 0

        return events
