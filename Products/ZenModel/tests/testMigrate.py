###########################################################################
#
# This program is part of Zenoss Core, an open source monitoring platform.
# Copyright (C) 2008, Zenoss Inc.
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 2 or (at your
# option) any later version as published by the Free Software Foundation.
#
# For complete information please visit: http://www.zenoss.com/oss/
#
###########################################################################

from ZenModelBaseTest import ZenModelBaseTest

from Products.ZenModel.migrate.Migrate import Migration, Version, Step

class MyTestStep(Step):
    def __init__(self, major, minor, micro):
        self.version = Version(major, minor, micro)
    def __cutover__(self):
        pass
    def __cleanup__(self):
        pass
    def name(self):
        return 'MyTestStep_%s' % self.version.short()

step300 = MyTestStep(3, 0, 0)
step30_70 = MyTestStep(3, 0, 70)
step310 = MyTestStep(3, 1, 0)
step31_70 = MyTestStep(3, 1, 70)
step320 = MyTestStep(3, 2, 0)
step321 = MyTestStep(3, 2, 1)
step32_70 = MyTestStep(3, 2, 70)
step400 = MyTestStep(4, 0, 0)
step401 = MyTestStep(4, 0, 1)
step402 = MyTestStep(4, 0, 2)
step40_70 = MyTestStep(4, 0, 70)
step410 = MyTestStep(4, 1, 0)
step411 = MyTestStep(4, 1, 1)
step41_70 = MyTestStep(4, 1, 70)
step420 = MyTestStep(4, 2, 0)

allsteps =  [
    step300, step30_70,
    step310, step31_70,
    step320, step321, step32_70,
    step400, step401, step402, step40_70,
    step410, step411, step41_70,
    step420,
    ]

realVersionSteps =  [
    step300,
    step310,
    step320, step321,
    step400, step401, step402,
    step410, step411,
    step420,
    ]

class TestMigrate(ZenModelBaseTest):
    def afterSetUp(self):
        super(TestMigrate, self).afterSetUp()

        self.version = Version(9,9,9)
        self.migration = Migration()
        self.oldCurrentVersion = self.migration._currentVersion
        self.migration._currentVersion = self.getTestVersion

    def getTestVersion(self):
        return self.version

    def assertIncludesVersions(self, steps, versions):
        includedVersions = map(lambda x:x.version, steps)
        for version in versions:
            if version not in includedVersions:
                self.assert_(False, "Version %s not included in steps: %s" %
                                    (version.short(), map(lambda x:x.name, steps)))

    def testStepDeterminationUpgrades(self):

        self.migration.allSteps = allsteps

        self.version = Version(99,99,99)
        steps = self.migration.determineSteps()
        self.assertEqual(len(steps), 0)

        self.version = Version(0,0,0)
        steps = self.migration.determineSteps()
        self.assertEqual(len(steps), len(allsteps))

        self.version = Version(4,1,1)
        steps = self.migration.determineSteps()
        self.assertEqual(len(steps), 2)
        self.assertIncludesVersions(steps,
            [x.version for x in (step420, step41_70)])

        self.version = Version(4,1,0)
        steps = self.migration.determineSteps()
        self.assertEqual(len(steps), 3)
        self.assertIncludesVersions(steps,
            [x.version for x in (step420, step41_70, step411)])

        self.version = Version(4,0,2)
        steps = self.migration.determineSteps()
        self.assertEqual(len(steps), 5)
        self.assertIncludesVersions(steps,
            [x.version for x in (step420, step41_70, step411, step410, step40_70)])

        self.version = Version(4,0,1)
        steps = self.migration.determineSteps()
        self.assertEqual(len(steps), 6)
        self.assertIncludesVersions(steps,
            [x.version for x in (step420, step41_70, step411, step410, step402, step40_70)])

        self.version = Version(4,0,0)
        steps = self.migration.determineSteps()
        self.assertEqual(len(steps), 7)
        self.assertIncludesVersions(steps,
            [x.version for x in (step420, step41_70, step411, step410, step402, step401, step40_70)])

    def testStepDeterminationInPlace(self):

        self.migration.allSteps = realVersionSteps

        self.version = Version(4,2,0)
        steps = self.migration.determineSteps()
        self.assertEqual(len(steps), 1)
        self.assertIncludesVersions(steps, [step420.version])

    def testGetEarliestAppropriateStepVersion(self):
        m = Migration(noopts=True)
        self.assertEquals(Version(1, 0, 70),
            m.getEarliestAppropriateStepVersion(codeVers=Version(1, 1, 50)))
        self.assertEquals(Version(1, 1, 70),
            m.getEarliestAppropriateStepVersion(codeVers=Version(1, 1, 70)))
        self.assertEquals(Version(1, 1, 70),
            m.getEarliestAppropriateStepVersion(codeVers=Version(1, 1, 99)))
        self.assertEquals(Version(1, 1, 70),
            m.getEarliestAppropriateStepVersion(codeVers=Version(1, 2, 0)))
        m.allSteps.append(MyTestStep(98, 3, 71))
        self.assertEquals(Version(98, 3, 70),
            m.getEarliestAppropriateStepVersion(codeVers=Version(99, 0, 1)))


    def testDetermineSteps(self):
        m = Migration(noopts=True)
        m.allSteps = [  MyTestStep(1, 0, 0),
                        MyTestStep(1, 1, 0),
                        MyTestStep(1, 2, 0),
                        ]
        m.options.level='1.1.0'
        self.assertEquals(m.determineSteps(), m.allSteps[1:])
        m.options.level = None
        m.options.steps = ['MyTestStep_1.1.0']
        self.assertEquals(m.determineSteps(), m.allSteps[1:2])


def test_suite():
    from unittest import TestSuite, makeSuite
    suite = TestSuite()
    suite.addTest(makeSuite(TestMigrate))
    return suite
        
