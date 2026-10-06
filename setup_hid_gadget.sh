#!/bin/bash
# Creates a USB composite HID gadget on the Pi Zero:
#   /dev/hidg0 = keyboard, /dev/hidg1 = consumer control (media/volume keys)
# Needs dtoverlay=dwc2 in config.txt and modules-load=dwc2 in cmdline.txt.
set -e
modprobe libcomposite
G=/sys/kernel/config/usb_gadget/deck

if [ -d "$G" ]; then
  echo "gadget already configured"
  exit 0
fi

mkdir -p "$G" && cd "$G"
echo 0x1d6b > idVendor
echo 0x0104 > idProduct
echo 0x0100 > bcdDevice
echo 0x0200 > bcdUSB

mkdir -p strings/0x409
echo "deck0001"            > strings/0x409/serialnumber
echo "DIY"                 > strings/0x409/manufacturer
echo "MML Keybow Deck"  > strings/0x409/product

mkdir -p configs/c.1/strings/0x409
echo "HID"  > configs/c.1/strings/0x409/configuration
echo 250    > configs/c.1/MaxPower

# --- keyboard (standard 8-byte boot report)
mkdir -p functions/hid.usb0
echo 1 > functions/hid.usb0/protocol
echo 1 > functions/hid.usb0/subclass
echo 8 > functions/hid.usb0/report_length
printf '\x05\x01\x09\x06\xa1\x01\x05\x07\x19\xe0\x29\xe7\x15\x00\x25\x01\x75\x01\x95\x08\x81\x02\x95\x01\x75\x08\x81\x03\x95\x05\x75\x01\x05\x08\x19\x01\x29\x05\x91\x02\x95\x01\x75\x03\x91\x03\x95\x06\x75\x08\x15\x00\x25\x65\x05\x07\x19\x00\x29\x65\x81\x00\xc0' \
  > functions/hid.usb0/report_desc

# --- consumer control (one 16-bit usage per report)
mkdir -p functions/hid.usb1
echo 0 > functions/hid.usb1/protocol
echo 0 > functions/hid.usb1/subclass
echo 2 > functions/hid.usb1/report_length
printf '\x05\x0c\x09\x01\xa1\x01\x15\x00\x26\xff\x03\x19\x00\x2a\xff\x03\x75\x10\x95\x01\x81\x00\xc0' \
  > functions/hid.usb1/report_desc

ln -s functions/hid.usb0 configs/c.1/
ln -s functions/hid.usb1 configs/c.1/

ls /sys/class/udc > UDC
echo "gadget enabled"
