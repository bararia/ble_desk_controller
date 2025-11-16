#!/usr/bin/env python

import asyncio
import sys
import threading
import time
import json
import shutil
from datetime import datetime
from bleak import BleakClient, BleakError
import numpy as np

# --- Configuration File Name ---
CONFIG_FILENAME = "config.json"

# --- ANSI Escape Codes ---
CURSOR_UP_N = lambda n: f"\033[{n}F"
CLEAR_LINE = "\033[K"

class DeskContext:
    """Thread safe class to hold shared state."""
    def __init__(self):
        self.current_mm = 0
        self.status = "Initializing..."
        self.lock = threading.Lock()
        self.quit_event = threading.Event()
        self.height_is_known_event = threading.Event()

    def set_status(self, new_status):
        with self.lock: self.status = new_status

    def set_height(self, new_height_mm):
        with self.lock:
            is_first_update = (self.current_mm == 0)
            self.current_mm = new_height_mm
            if is_first_update and new_height_mm != 0:
                self.height_is_known_event.set()

    def get_data(self):
        with self.lock: return self.status, self.current_mm / 10.0
            
    def should_quit(self):
        return self.quit_event.is_set()

# -----------------------------------------------------------------
# BLUETOOTH LOGIC
# -----------------------------------------------------------------

def notification_handler(sender, data: bytearray, context: DeskContext):
    hex_data = data.hex()
    try:
        index = hex_data.index("f2f20103")
        height_hex = hex_data[index + 8 : index + 12]
        new_height_mm = int(height_hex, 16)
        if new_height_mm != context.current_mm:
            is_first_update = (context.current_mm == 0)
            context.set_height(new_height_mm)
            if is_first_update and new_height_mm != 0:
                context.height_is_known_event.set()
    except ValueError: pass
    except Exception as e: context.set_status(f"Parse Error: {e}")

async def wait_for_settle(duration_s, context):
    """Wait for a duration, checking for quit signal."""
    start = time.time()
    while time.time() - start < duration_s:
        if context.should_quit():
            return False
        await asyncio.sleep(0.1)
    return True

async def move_to_start_pos(client, context, write_uuid, cmd, target_mm, is_moving_up):
    """Moves desk to the starting position before a test."""
    if is_moving_up:
        while context.current_mm < target_mm and not context.should_quit():
            await client.write_gatt_char(write_uuid, cmd, response=False)
            await asyncio.sleep(0.1)
    else:
        while context.current_mm > target_mm and not context.should_quit():
            await client.write_gatt_char(write_uuid, cmd, response=False)
            await asyncio.sleep(0.1)
    
    await client.write_gatt_char(write_uuid, cmd, response=False) 

async def run_overshoot_test(client: BleakClient, context: DeskContext, config: dict, commands: dict, setpoint_mm):
    """
    Directly measures coasting distance for UP and DOWN.
    """
    write_uuid = config["write_uuid"]
    cmd_up = commands["move_up"]
    cmd_down = commands["move_down"]
    cmd_stop = commands["stop"]
    
    overshoots_up = []
    overshoots_down = []
    
    # Autotune parameters
    NUM_TESTS = 3
    START_MARGIN_MM = 50 

    try:
        # --- TEST MOVING UP ---
        for i in range(NUM_TESTS):
            if context.should_quit(): return None
            
            # Go to start position (below setpoint)
            start_pos_mm = setpoint_mm - START_MARGIN_MM
            context.set_status(f"UP Test {i+1}/{NUM_TESTS}: Moving to start pos ({start_pos_mm/10.0} cm)...")
            await move_to_start_pos(client, context, write_uuid, cmd_down, start_pos_mm + 5, False)
            await wait_for_settle(1.5, context)
            await move_to_start_pos(client, context, write_uuid, cmd_up, start_pos_mm, True)
            await client.write_gatt_char(write_uuid, cmd_stop, response=False)
            if not await wait_for_settle(1.5, context): return None

            # Start test
            context.set_status(f"UP Test {i+1}/{NUM_TESTS}: Moving UP to {setpoint_mm/10.0} cm...")
            while context.current_mm < setpoint_mm and not context.should_quit():
                await client.write_gatt_char(write_uuid, cmd_up, response=False)
                await asyncio.sleep(0.1)
            
            # Stop exactly at the setpoint
            await client.write_gatt_char(write_uuid, cmd_stop, response=False)
            context.set_status(f"UP Test {i+1}/{NUM_TESTS}: Stopped. Measuring coast...")
            if not await wait_for_settle(2.0, context): return None # Wait for coast
            
            overshoot = context.current_mm - setpoint_mm
            overshoots_up.append(overshoot)
            context.set_status(f"UP Test {i+1}/{NUM_TESTS}: Coasted {overshoot} mm")
            await asyncio.sleep(1.0)

        # --- TEST MOVING DOWN ---
        for i in range(NUM_TESTS):
            if context.should_quit(): return None

            # Go to start position (above setpoint)
            start_pos_mm = setpoint_mm + START_MARGIN_MM
            context.set_status(f"DOWN Test {i+1}/{NUM_TESTS}: Moving to start pos ({start_pos_mm/10.0} cm)...")
            await move_to_start_pos(client, context, write_uuid, cmd_up, start_pos_mm - 5, True)
            await wait_for_settle(1.5, context)
            await move_to_start_pos(client, context, write_uuid, cmd_down, start_pos_mm, False)
            await client.write_gatt_char(write_uuid, cmd_stop, response=False)
            if not await wait_for_settle(1.5, context): return None
            
            # Start test
            context.set_status(f"DOWN Test {i+1}/{NUM_TESTS}: Moving DOWN to {setpoint_mm/10.0} cm...")
            while context.current_mm > setpoint_mm and not context.should_quit():
                await client.write_gatt_char(write_uuid, cmd_down, response=False)
                await asyncio.sleep(0.1)

            # Stop exactly at the setpoint
            await client.write_gatt_char(write_uuid, cmd_stop, response=False)
            context.set_status(f"DOWN Test {i+1}/{NUM_TESTS}: Stopped. Measuring coast...")
            if not await wait_for_settle(2.0, context): return None

            overshoot = setpoint_mm - context.current_mm
            overshoots_down.append(overshoot)
            context.set_status(f"DOWN Test {i+1}/{NUM_TESTS}: Coasted {overshoot} mm")
            await asyncio.sleep(1.0)
        
        # --- CALCULATE RESULTS ---
        avg_overshoot_up = sum(overshoots_up) / len(overshoots_up)
        avg_overshoot_down = sum(overshoots_down) / len(overshoots_down)
        
        context.set_status("Autotune Complete.")
        return (avg_overshoot_up, avg_overshoot_down)

    except Exception as e:
        context.set_status(f"Error in test: {e}")
        return None
    finally:
        await client.write_gatt_char(write_uuid, cmd_stop, response=False)
        context.quit_event.set()

async def async_ble_main(context: DeskContext, config: dict, commands: dict, setpoint_mm):
    client = None
    results = None
    try:
        device_address = config["device_address"]
        write_uuid = config["write_uuid"]
        notify_uuid = config["notify_uuid"]
        
        context.set_status(f"Scanning for {device_address}...")
        client = BleakClient(device_address)
        await client.connect(timeout=10.0)
        
        context.set_status("Connected. Waking desk...")
        await client.write_gatt_char(write_uuid, commands["stop"], response=False)
        await asyncio.sleep(0.2)
        
        context.set_status("Starting height listener...")
        await client.start_notify(
            notify_uuid,
            lambda sender, data: notification_handler(sender, data, context)
        )
        
        context.set_status("Waking desk & getting initial height...")
        await client.write_gatt_char(write_uuid, commands["fetch_height"], response=False)
        
        # Wait for the first height reading
        if not context.height_is_known_event.wait(timeout=10.0):
            context.set_status("Error: No initial height received.")
            raise Exception("Desk did not report height.")

        # Run the main autotune task
        results = await run_overshoot_test(client, context, config, commands, setpoint_mm)
        
    except BleakError as e:
        context.set_status(f"BleakError: {e}")
    except Exception as e:
        context.set_status(f"Error: {e}")
    finally:
        if client and client.is_connected:
            context.set_status("Disconnecting...")
            await client.stop_notify(config["notify_uuid"])
            await client.disconnect()
        context.set_status("Disconnected.")
        context.quit_event.set()
    return results

def run_ble_logic(context: DeskContext, config: dict, commands: dict, setpoint_mm):
    """Entry point for the BLE thread"""
    try:
        return asyncio.run(async_ble_main(context, config, commands, setpoint_mm))
    except Exception as e:
        context.set_status(f"BLE Thread Error: {e}")
        return None

# -----------------------------------------------------------------
# ASCII UI LOGIC (Runs in the Main thread)
# -----------------------------------------------------------------

def draw_ascii_ui(context: DeskContext, ble_thread: threading.Thread):
    UI_LINES = 7
    print("\n" * UI_LINES)
    
    try:
        while ble_thread.is_alive() and not context.should_quit():
            status, current_cm = context.get_data()
            
            ui_string = (
                f"--- PID Autotune Running ---{CLEAR_LINE}\n"
                f"{CLEAR_LINE}\n"
                f"  Current Height: {current_cm:.1f} cm{CLEAR_LINE}\n"
                f"{CLEAR_LINE}\n"
                f"  Status: {status}{CLEAR_LINE}\n"
                f"{CLEAR_LINE}\n"
                f"  (Press Ctrl+C to stop){CLEAR_LINE}\n"
            )
            print(CURSOR_UP_N(UI_LINES), end="")
            print(ui_string, end="")
            time.sleep(0.05)

    except KeyboardInterrupt:
        print("\nStopping test...")
        context.set_status("Manual quit... disconnecting...")
        context.quit_event.set()

    finally:
        ble_thread.join()
        print(CURSOR_UP_N(UI_LINES), end="")
        for _ in range(UI_LINES): print(f"{CLEAR_LINE}")
        print("Autotune process finished.")

# -----------------------------------------------------------------
# MAIN FUNCTION
# -----------------------------------------------------------------

def main():
    try:
        import numpy as np
    except ImportError:
        print("Error: This script requires the 'numpy' library.")
        print("Please install it: pip3 install numpy")
        sys.exit(1)

    if len(sys.argv) != 2:
        print("Error: Invalid arguments.")
        print(f"Usage: sudo python3 {sys.argv[0]} <setpoint_cm>")
        print(f"Example: sudo python3 {sys.argv[0]} 90.0")
        sys.exit(1)

    config_path = CONFIG_FILENAME
    
    # --- Load Config File ---
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
    except FileNotFoundError:
        print(f"Error: Config file '{config_path}' not found.")
        print("Please make sure 'config.json' is in the same directory.")
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"Error: Could not parse '{config_path}'. Is it valid JSON?")
        sys.exit(1)

    # --- Validate Setpoint Argument ---
    try:
        setpoint_cm = float(sys.argv[1])
        min_cm = config["height_limits"]["min_cm"]
        max_cm = config["height_limits"]["max_cm"]
        if not (min_cm + 10 <= setpoint_cm <= max_cm - 10):
            print(f"Error: Setpoint must be at least 10cm within the height limits ({min_cm}-{max_cm} cm).")
            print(f"Please choose a setpoint between {min_cm+10} and {max_cm-10}.")
            sys.exit(1)
        setpoint_mm = int(round(setpoint_cm * 10))
    except ValueError:
        print(f"Error: '{sys.argv[1]}' is not a valid height.")
        sys.exit(1)
    
    # --- Convert Hex Commands to Bytes ---
    try:
        commands_hex = config["commands"]
        commands_bytes = {name: bytes.fromhex(cmd) for name, cmd in commands_hex.items()}
    except Exception as e:
        print(f"Error converting commands in config file: {e}")
        sys.exit(1)
        
    # --- Start Application ---
    context = DeskContext()
    
    results = None
    def ble_thread_wrapper():
        nonlocal results
        results = run_ble_logic(context, config, commands_bytes, setpoint_mm)

    ble_thread = threading.Thread(target=ble_thread_wrapper)
    ble_thread.start()
    
    draw_ascii_ui(context, ble_thread)
    
    # --- Handle Results and Update Config ---
    if results:
        avg_up, avg_down = results
        
        print("\n--- Autotune Results ---")
        print(f"  Avg. UP Overshoot:   {avg_up/10.0:.2f} cm ({avg_up:.0f} mm)")
        print(f"  Avg. DOWN Overshoot: {avg_down/10.0:.2f} cm ({avg_down:.0f} mm)")
        
        try:
            choice = input("\nDo you want to update 'config.json' with these values? (y/n): ").strip().lower()
            if choice == 'y':
                now = datetime.now().strftime("%Y-%m-%d_%H%M%S")
                backup_path = f"{config_path}.{now}.bak"
                shutil.copy(config_path, backup_path)
                print(f"\nBackup of original config saved to:\n{backup_path}")

                with open(config_path, 'r') as f:
                    config_data = json.load(f)
                
                config_data["tuning_params"]["overshoot_mm_up"] = int(round(avg_up))
                config_data["tuning_params"]["overshoot_mm_down"] = int(round(avg_down))
                
                with open(config_path, 'w') as f:
                    json.dump(config_data, f, indent=4)
                    
                print(f"\nSuccessfully updated '{config_path}' with new parameters.")
            else:
                print("\nConfig file not updated.")
        except Exception as e:
            print(f"\nError updating config file: {e}")
    else:
        print("Autotune was cancelled or failed. Config file not updated.")

if __name__ == "__main__":
    main()
    