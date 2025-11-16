# Python BLE Standing Desk Controller

This project allows you to control a BLE-enabled standing desk (like those using a Jiecang controller) from the command line.

It uses a PID-style control loop to move to a specific height with high accuracy, compensating for motor "coasting" or "overshoot." 
It's configured via a simple `config.json` file, so it should be adaptable to other desks.

Tested to be  working with:
1. Handset JCHT35K24C (https://www.jiecang.com/product/jcht35k24c.html)

## Key Features

* **Move to precise height:** `sudo python3 move_smart_cli.py 95.5`
* **PID-style "Smart" Movement:** A multi-stage loop ensures accuracy.
    1.  **Fast Approach:** Moves at full speed to just before the target.
    2.  **Settle:** Waits for the desk to stop coasting.
    3.  **Nudge & Correct:** Uses tiny "nudges" to hit the target with millimeter precision.
* **Autotune Script:** Includes a script to automatically test your desk's physics and find the perfect tuning parameters.
* **Config File Based:** All device addresses, UUIDs, and tuning parameters are in `config.json`, not hard-coded.

## Installation

### 1. Prerequisites

You must be on a Linux system with Python 3 and the BlueZ Bluetooth stack.

```bash
# Install Python 3, pip, and the core Bluetooth development library
sudo apt update
sudo apt install python3 python3-pip libbluetooth-dev

2. Install Project
Bash

# 1. Clone this repository
git clone [https://github.com/bararia/ble_desk_controller.git](https://github.com/bararia/ble_desk_controller.git)
cd ble_desk_controller

# 2. Install the required Python libraries
pip3 install -r requirements.txt


️How to Use (In 2 Steps)
Step 1: Calibrate Your Desk (Autotune)
You must/should do this once. 
This script will find your desk's unique "coasting" distance (overshoot) so the main script can be accurate.
This is usually due to the weight of the stuff thats on the desk so it will be different for different
configurations.

Make sure your desk is on and not connected to your phone.

Run the autotune.py script. You need to provide:

Make sure that your desk is at a normal height (80-85cm)

A target height to test around (e.g., 95.0).

Bash

# This will move the desk up and down 3 times to get an average.
sudo python3 autotune.py 95.0
The script will run for a few minutes. When it finishes, it will show you the measured overshoot values:

--- Autotune Results ---
  Avg. UP Overshoot:   1.00 cm (10 mm)
  Avg. DOWN Overshoot: 1.70 cm (17 mm)
It will then ask you to save these values. Press y and then Enter.

Do you want to update 'config.json' with these values? (y/n): y
It will create a backup and update config.json with the new, tuned parameters.

Step 2: Move Your Desk
Now that your script is calibrated, you can move your desk to any height.

Run the move_smart_cli.py script and the target height in centimeters.

Bash

# Move to 85.5 cm
sudo python3 move_smart_cli.py 85.5

# Move to 102.0 cm
sudo python3 move_smart_cli.py 102
The script will connect, move the desk, and automatically exit when the target height is reached.

How It Works (For Developers)
This project works by sending specific byte commands to the desk's BLE controller. 
These were found by reverse-engineering the over the air BLE packets using a nRF52840
dongle, wireshark, hidtools, bluez tools.

Write Characteristic (...fe61): Used to send commands.

Notify Characteristic (...fe62): Used to receive height updates.

Key Commands: 
|-----------------------------------------------------------------------| 
| Command 	| Hex Code 	| Description 				|
|-----------------------------------------------------------------------| 
| Move Up 	| F1F10100017E 	| Moves desk up (must be looped) 	| 
| Move Down 	| F1F10200027E 	| Moves desk down (must be looped) 	| 
| Stop 		| F1F1L2B002B7E | Stops movement (also wakes desk) 	| 
| Get Height 	| F1F10700077E 	| Requests a height update packet 	|
|-----------------------------------------------------------------------| 

Height Packet: The desk responds on the notify characteristic with a packet containing ...f2f20103... followed by the height as a 2-byte hex value (in millimeters).
