#! /usr/bin/env bash   
##############################################################################
#
# Copyright (C) Zenoss, Inc. 2014, all rights reserved.
#
# This content is made available according to terms specified in
# License.zenoss under the directory where your Zenoss product is installed.
#
##############################################################################

checkRC() {
    [[ $1 != 0 ]] && exit "$1"
}

doUpgrade() {
    zenpack --restore
    checkRC $?
    zenmigrate
    checkRC $?
}

