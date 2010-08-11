###########################################################################
#
# This program is part of Zenoss Core, an open source monitoring platform.
# Copyright (C) 2009 Zenoss Inc.
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 2 as published by
# the Free Software Foundation.
#
# For complete information please visit: http://www.zenoss.com/oss/
#
###########################################################################

from twisted.internet import defer
from twisted.spread import pb

from Acquisition import aq_parent
from Products.ZenHub.HubService import HubService
from Products.ZenHub.PBDaemon import translateError
from Products.ZenHub.services.Procrastinator import Procrastinate
from Products.ZenHub.services.ThresholdMixin import ThresholdMixin
from Products.ZenHub.zodb import onUpdate, onDelete

from Products.ZenModel.Device import Device
from Products.ZenModel.DeviceClass import DeviceClass
from Products.ZenModel.PerformanceConf import PerformanceConf
from Products.ZenModel.ZenPack import ZenPack


class DeviceProxy(pb.Copyable, pb.RemoteCopy):
    def __init__(self):
        """
        Do not use base classes initializers
        """

pb.setUnjellyableForClass(DeviceProxy, DeviceProxy)


# TODO: doc me!
BASE_ATTRIBUTES = ('id',
                   'manageIp',
                   )


class CollectorConfigService(HubService, ThresholdMixin):
    def __init__(self, dmd, instance, deviceProxyAttributes=()):
        """
        Constructs a new CollectorConfig instance.
        
        Subclasses must call this __init__ method but cannot do so with 
        the super() since parents of this class are not new-style classes.

        @param dmd: the Zenoss DMD reference
        @param instance: the collector instance name
        @param deviceProxyAttributes: a tuple of names for device attributes
               that should be copied to every device proxy created
        @type deviceProxyAttributes: tuple
        """
        HubService.__init__(self, dmd, instance)

        self._deviceProxyAttributes = BASE_ATTRIBUTES + deviceProxyAttributes

        # TODO: wtf?
        self._prefs = self.dmd.Monitors.Performance._getOb(self.instance)
        self.config = self._prefs # TODO fix me, needed for ThresholdMixin

        # TODO: what's this for?
        self._procrastinator = Procrastinate(self._pushConfig)
        self._reconfigProcrastinator = Procrastinate(self._pushReconfigure)

    def _wrapFunction(self, functor, *args, **kwargs):
        """
        Call the functor using the arguments, and trap any unhandled exceptions.

        @parameter functor: function to call
        @type functor: method
        @parameter args: positional arguments
        @type args: array of arguments
        @parameter kwargs: keyword arguments
        @type kwargs: dictionary
        @return: result of functor(*args, **kwargs) or None if failure
        @rtype: result of functor
        """
        try:
            return functor(*args, **kwargs)
        except (SystemExit, KeyboardInterrupt): raise
        except Exception, ex:
            msg = 'Unhandled exception in zenhub service %s: %s' % (
                      self.__class__, str(ex))
            self.log.exception(msg)

            import traceback
            from Products.ZenEvents.ZenEventClasses import Critical

            evt = dict(
                severity=Critical,
                component=str(self.__class__),
                traceback=traceback.format_exc(),
                summary=msg,
                device=self.instance,
                methodCall="%s(%s, %s)" % (functor.__name__, args, kwargs)
            )
            self.sendEvent(evt)
        return None

    @onUpdate(PerformanceConf)
    def perfConfUpdated(self, object, event):
        if object.id == self.instance:
            for listener in self.listeners:
                listener.callRemote('setPropertyItems', object.propertyItems())

    @onUpdate(ZenPack)
    def zenPackUpdated(self, object, event):
        for listener in self.listeners:
            try:
                listener.callRemote('updateThresholdClasses',
                                    self.remote_getThresholdClasses())
            except Exception, ex:
                self.log.warning("Error notifying a listener of new classes")

    @onUpdate(Device)
    def deviceUpdated(self, object, event):
        self._notifyAll(object)

    @onUpdate(None) # Matches all
    def notifyAffectedDevices(self, object, event):
        # FIXME: This is horrible

        if isinstance(object, self._getNotifiableClasses()):
            self._reconfigureIfNotify(object)

        else:
            if isinstance(object, Device):
                return
            # something else... mark the devices as out-of-date
            while object:
                # walk up until you hit an organizer or a device
                if isinstance(object, DeviceClass):
                    for device in object.getSubDevices():
                        self._notifyAll(device)
                    break

                if isinstance(object, Device):
                    self._notifyAll(object)
                    break

                object = aq_parent(object)

    @onDelete(Device)
    def deviceDeleted(self, object, event):
        devid = object.id
        for listener in self.listeners:
            listener.callRemove('deleteDevice', devid)


    @translateError
    def remote_getConfigProperties(self):
        return self._prefs.propertyItems()

    @translateError
    def remote_getDeviceConfigs(self, deviceNames = None):
        if not deviceNames:
            devices = self._prefs.devices()
        else:
            devices = []
            for name in deviceNames:
                device = self.dmd.Devices.findDeviceByIdExact(name)
                if not device:
                    continue
                else:
                    devices.append(device)

        devices = self._filterDevices(devices)

        deviceConfigs = []
        for device in devices:
            deviceConfig = self._wrapFunction(self._createDeviceProxy, device)
            if deviceConfig:
                deviceConfigs.append(deviceConfig)

        self._wrapFunction(self._postCreateDeviceProxy, deviceConfigs)
        return deviceConfigs

    def _postCreateDeviceProxy(self, deviceConfigs):
        pass

    def _createDeviceProxy(self, device):
        """
        Creates a device proxy object that may be copied across the network.

        Subclasses should override this method, call it for a basic DeviceProxy
        instance, and then add any additional data to the proxy as their needs
        require.

        @param device: the regular device object to create a proxy from
        @return: a new device proxy object, or None if no proxy can be created
        @rtype: DeviceProxy
        """
        proxy = DeviceProxy()

        # copy over all the attributes requested
        for attrName in self._deviceProxyAttributes:
            setattr(proxy, attrName, getattr(device, attrName, None))

        return proxy

    def _filterDevice(self, device):
        """
        Determines if the specified device should be included for consideration
        in being sent to the remote collector client.

        Subclasses should override this method, call it for the default
        filtering behavior, and then add any additional filtering as needed.

        @param device: the device object to filter
        @return: True if this device should be included for further processing
        @rtype: boolean
        """
        return device.monitorDevice()

    def _filterDevices(self, devices):
        """
        Filters out devices from the provided list that should not be
        converted into DeviceProxy instances and sent back to the collector
        client.
        
        @param device: the device object to filter
        @return: a list of devices that are to be included
        @rtype: list
        """
        filteredDevices = []

        for device in devices:
            device = device.primaryAq() # still black magic to me...

            if self._filterDevice(device):
                filteredDevices.append(device)
                self.log.debug("Device %s included by filter", device.id)
            else:
                self.log.debug("Device %s excluded by filter", device.id)

        return filteredDevices

    def _notifyAll(self, object):
        """
        TODO
        """
        # TODO doc me
        if object.perfServer.getRelatedId() == self.instance:
            self._procrastinator.doLater(object)

    def _pushConfig(self, device):
        """
        TODO
        """
        deferreds = []

        if self._filterDevice(device):
            proxy = self._wrapFunction(self._createDeviceProxy, device)
            if proxy:
                self._wrapFunction(self._postCreateDeviceProxy, [proxy])
        else:
            proxy = None

        for listener in self.listeners:
            if proxy is None:
                deferreds.append(listener.callRemote('deleteDevice', device.id))
            else:
                deferreds.append(self._sendDeviceProxy(listener, proxy))

        return defer.DeferredList(deferreds)

    def _sendDeviceProxy(self, listener, proxy):
        """
        TODO
        """
        return listener.callRemote('updateDeviceConfig', proxy)

    # FIXME: Don't use _getNotifiableClasses, use @onUpdate(myclasses)
    def _getNotifiableClasses(self):
        """
        a tuple of classes. When any object of a type in the sequence is 
        modified the collector connected to the service will be notified to 
        update its configuration

        @rtype: tuple
        """
        return ()

    def _pushReconfigure(self, value):
        """
        notify the collector to reread the entire configuration
        """
        #value is unused but needed for the procrastinator framework
        for listener in self.listeners:
            listener.callRemote('notifyConfigChanged')
        self._reconfigProcrastinator.clear()

    def _reconfigureIfNotify(self, object):
        if self._notifyConfigChange(object):
            self.log.debug('scheduling collector reconfigure')
            self._reconfigProcrastinator.doLater(True)

    def _notifyConfigChange(self, object):
        """
        Called when an object of a type from _getNotifiableClasses is 
        encountered
        @return: should a notify config changed be sent
        @rtype: boolean
        """
        return True
