#!/system/bin/sh
set -eu
mount -t tmpfs -o mode=0755 tmpfs /debug_ramdisk
mkdir -p /debug_ramdisk/.magisk/device /debug_ramdisk/.magisk/mirror /debug_ramdisk/.magisk/block /data/adb/magisk
cp /system/etc/impad-magisk/* /data/adb/magisk/
cp /system/etc/impad-magisk/magisk /debug_ramdisk/magisk
cp /system/etc/impad-magisk/magiskpolicy /debug_ramdisk/magiskpolicy
chmod -R 755 /debug_ramdisk /data/adb/magisk
ln -s magisk /debug_ramdisk/su
ln -s magisk /debug_ramdisk/resetprop
/debug_ramdisk/magisk --daemon
/debug_ramdisk/magisk --post-fs-data
