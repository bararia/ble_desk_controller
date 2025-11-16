import asyncio
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

# These are the "fingerprint" UUIDs the app searches for
KNOWN_DESK_UUIDS = {
    "0000ff12-0000-1000-8000-00805f9b34fb", 
    "0000fe60-0000-1000-8000-00805f9b34fb" 
}

# A set to keep track of devices we've already found
found_devices = set()

def detection_callback(device: BLEDevice, advertisement_data: AdvertisementData):
    """
    This function is called for every BLE device found.
    We will check its "fingerprint" (Service UUIDs).
    """
    
    # If we've already printed this device, skip it
    if device.address in found_devices:
        return

    # Check if the device is advertising any service UUIDs
    if advertisement_data.service_uuids:
        for uuid in advertisement_data.service_uuids:
            # Check if the advertised UUID is one of the ones we know
            if str(uuid).lower() in KNOWN_DESK_UUIDS:
                
                print(f"--- Found a Desk Controller! ---")
                print(f"  Name:    {device.name or 'Unknown'}")
                print(f"  Address: {device.address}")
                print(f"  RSSI:    {advertisement_data.rssi} dBm")
                print(f"  UUID:    {uuid}")
                print("----------------------------------\n")
                
                # Add to our set so we don't print it again
                found_devices.add(device.address)
                break # Stop checking other UUIDs for this device

async def main():
    """
    Runs the scanner.
    """
    print("Scanning for desk controllers for 10 seconds...")
    print("Make sure your desk is powered on and not connected to your phone.\n")
    
    # Create a scanner that uses our callback function
    scanner = BleakScanner(detection_callback=detection_callback)
    
    # Start the scanner
    await scanner.start()
    
    # Let it run for 10 seconds
    await asyncio.sleep(10.0)
    
    # Stop the scanner
    await scanner.stop()

    if not found_devices:
        print("No desk controllers were found.")

if __name__ == "__main__":
    # On Linux, you must run this with sudo
    # sudo python3 find_desks.py
    asyncio.run(main())