#!/bin/bash
# Starts the capture listener with the full New Life channel map.
# Edit this file if the BlackHole routing/channel assignments ever change.
cd "$(dirname "$0")"
source .venv/bin/activate
python3 usb_capture_listener.py --device "BlackHole 64ch" --channels "1:Master,3:LeadMic,4:Preach,5:VocSum,6:WLVoc,7:KeysVoc,8:RightVoc,9:BandSum,10:Bass,11:ElGt,13:Acoustic,14:Keys,16:DrSum,17:Kick,18:Snare,19:HiHat,20:FloorTom,21:OverheadL,22:OverheadR,23:EDrumL,24:EDrumR,25:AudienceMic"
