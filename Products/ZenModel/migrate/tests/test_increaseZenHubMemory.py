##############################################################################
#
# Copyright (C) Zenoss, Inc. 2018, all rights reserved.
#
# This content is made available according to terms specified in
# License.zenoss under the directory where your Zenoss product is installed.
#
##############################################################################

import common
import unittest


class test_increaseZenHubMemory(unittest.TestCase, common.ServiceMigrationTestCase):
    """
    Test updating memcached ramCommitment
    """
    initial_servicedef = 'zenoss-cse-7.0.3_330.json'
    expected_servicedef = 'zenoss-cse-7.0.3_330-increaseZenHubMemory.json'
    migration_module_name = 'increaseZenHubMemory'
    migration_class_name = 'IncreaseZenHubMemory'

if __name__ == '__main__':
    unittest.main()
