##############################################################################
#
# Copyright (C) Zenoss, Inc. 2013, all rights reserved.
#
# This content is made available according to terms specified in
# License.zenoss under the directory where your Zenoss product is installed.
#
##############################################################################

from zope.interface import implementer
from .interfaces import IControlPlaneClient


@implementer(IControlPlaneClient)
class ControlPlaneClient(object):
    """
    """

    def queryServices(self, **kwargs):
        """
        """

    def getService(self, instanceId):
        """
        """
        # get data from url
        # app = json.loads(jsondata)

    def updateService(self, instance):
        """
        """

    def getServiceLog(self, uri, start=0, end=None):
        """
        """

    def getServiceConfiguration(self, uri):
        """
        """

    def updateServiceConfiguration(self, uri, config):
        """
        """
