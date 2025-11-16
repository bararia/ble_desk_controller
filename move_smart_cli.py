#!/usr/bin/env python3

import asyncio
import sys
import threading
import time
import json
from bleak import BleakClient, BleakError

# --- Configuration File Name ---
CONFIG_FILENAME = "config.json"

# --- ANSI Escape Codes ---
CURSOR_UP_N = lambda n: f"\033[{n}F"
CLEAR_LINE = "\033[K"

class DeskContext:
    """
    A thread-safe class to hold all shared state
    between the UI and BLE threads.
    """
    def __init__(self, target_cm):
        self.target_mm = int(round(target_cm * 10))
        self.current_mm = 0
        self.status = "Initializing..."
        self.error_mm = 0
        self.is_moving = True
        
        self.lock = threading.Lock()
        self.quit_event = threading.Event()
        self.height_is_known_event = threading.Event()

    def set_status(self, new_status):
        with self.lock: self.status = new_status

    def set_height(self, new_height_mm):
        with self.lock:
            self.current_mm = new_height_mm
            self.error_mm = self.target_mm - self.current_mm

    def get_display_data(self):
        with self.lock:
            return (
                self.status,
                self.current_mm / 10.0,
                self.target_mm / 10.0,
                self.error_mm / 10.0
            )
            
    def should_quit(self):
        return self.quit_event.is_set()

# -----------------------------------------------------------------
# BLUETOOTH LOGIC (Runs in a separate thread)
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

async def move_task(client: BleakClient, context: DeskContext, config: dict, commands: dict):
    """The main PID control loop"""
    try:
        # Load parameters from config
        params = config["tuning_params"]
        overshoot_mm_up = params["overshoot_mm_up"]
        overshoot_mm_down = params["overshoot_mm_down"]
        final_margin_mm = params["final_margin_mm"]
        nudge_coarse_s = params["nudge_coarse_s"]
        nudge_fine_s = params["nudge_fine_s"]
        settle_time_s = params["settle_time_s"]
        nudge_limit = params["nudge_limit"]
        
        write_uuid = config["write_uuid"]

        context.set_status("Waiting for initial height...")
        start_time = time.time()
        while context.current_mm == 0:
            if context.should_quit(): return
            if time.time() - start_time > 10:
                context.set_status("Error: No height data. Is desk on?")
                return
            await asyncio.sleep(0.1)
        
        if context.current_mm > context.target_mm:
            direction, cmd, overshoot_mm = 'DOWN', commands["move_down"], overshoot_mm_down
            while context.current_mm > (context.target_mm + overshoot_mm) and not context.should_quit():
                context.set_status(f"Moving DOWN... (Compensation: {overshoot_mm}mm)")
                await client.write_gatt_char(write_uuid, cmd, response=False)
                await asyncio.sleep(0.1)
        else:
            direction, cmd, overshoot_mm = 'UP', commands["move_up"], overshoot_mm_up
            while context.current_mm < (context.target_mm - overshoot_mm) and not context.should_quit():
                context.set_status(f"Moving UP... (Compensation: {overshoot_mm}mm)")
                await client.write_gatt_char(write_uuid, cmd, response=False)
                await asyncio.sleep(0.1)
        
        if context.should_quit(): return
        
        context.set_status(f"Fast approach complete. Stopping...")
        await client.write_gatt_char(write_uuid, commands["stop"], response=False)
        await asyncio.sleep(0.1)
        await client.write_gatt_char(write_uuid, commands["stop"], response=False)
        
        nudge_count = 0
        while abs(context.error_mm) > final_margin_mm and nudge_count < nudge_limit and not context.should_quit():
            nudge_count += 1
            context.set_status(f"Waiting to settle... (Nudge {nudge_count}/{nudge_limit})")
            await asyncio.sleep(settle_time_s)
            
            error_mm = context.error_mm
            if abs(error_mm) <= final_margin_mm: break

            nudge_duration_s = nudge_fine_s if abs(error_mm) <= 5 else nudge_coarse_s
            status_detail = "Fine 50ms" if nudge_duration_s == nudge_fine_s else "Coarse 100ms"

            if error_mm > 0:
                context.set_status(f"Nudging UP... ({status_detail})")
                await client.write_gatt_char(write_uuid, commands["move_up"], response=False)
            else:
                context.set_status(f"Nudging DOWN... ({status_detail})")
                await client.write_gatt_char(write_uuid, commands["move_down"], response=False)
            
            await asyncio.sleep(nudge_duration_s)
            await client.write_gatt_char(write_uuid, commands["stop"], response=False)

        context.set_status("Target height reached. Complete.")
        
    except Exception as e:
        context.set_status(f"Error in move_task: {e}")
    finally:
        context.is_moving = False
        await client.write_gatt_char(config["write_uuid"], commands["stop"], response=False)
        context.quit_event.set()

async def async_ble_main(context: DeskContext, config: dict, commands: dict):
    client = None
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
        
        context.set_status("Reading current height...")
        await client.write_gatt_char(write_uuid, commands["fetch_height"], response=False)
        await asyncio.sleep(0.1)
        await client.write_gatt_char(write_uuid, commands["fetch_height"], response=False)
        
        await move_task(client, context, config, commands)
        
        while not context.should_quit():
             await asyncio.sleep(0.1)
             
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
        context.is_moving = False
        context.quit_event.set()

def run_ble_logic(context: DeskContext, config: dict, commands: dict):
    try:
        asyncio.run(async_ble_main(context, config, commands))
    except Exception as e:
        context.set_status(f"BLE Thread Error: {e}")

# -----------------------------------------------------------------
# ASCII UI LOGIC (Runs in the Main thread)
# -----------------------------------------------------------------

def draw_ascii_ui(context: DeskContext, ble_thread: threading.Thread):
    """
    Main UI loop for the regular terminal.
    """
    UI_LINES = 8
    print("\n" * UI_LINES)
    
    try:
        while not context.should_quit():
            status, current_cm, target_cm, error_cm = context.get_display_data()
            ui_string = (
                f"--- Desk Controller ---{CLEAR_LINE}\n"
                f"{CLEAR_LINE}\n"
                f"  Target:  {target_cm:.1f} cm{CLEAR_LINE}\n"
                f"  Current: {current_cm:.1f} cm{CLEAR_LINE}\n"
                f"  Error:   {error_cm:.1f} cm  {CLEAR_LINE}\n"
                f"{CLEAR_LINE}\n"
                f"  Status: {status}{CLEAR_LINE}\n"
                f"  (Press Ctrl+C to quit){CLEAR_LINE}\n"
            )
            print(CURSOR_UP_N(UI_LINES), end="")
            print(ui_string, end="")
            time.sleep(0.05)
    except KeyboardInterrupt:
        context.set_status("Manual quit... disconnecting...")
        context.quit_event.set()
    finally:
        ble_thread.join()
        print(CURSOR_UP_N(UI_LINES), end="")
        for _ in range(UI_LINES): print(f"{CLEAR_LINE}")
        print("Disconnected. Exiting.")

# -----------------------------------------------------------------
# MAIN FUNCTION
# -----------------------------------------------------------------

def main():
    """
    Main entry point: loads config, validates input, and starts threads.
    """
    # --- Argument Parsing ---
    if len(sys.argv) != 2:
        print("Error: Invalid arguments.")
        print(f"Usage: sudo python3 {sys.argv[0]} <height_in_cm>")
        print(f"Example: sudo python3 {sys.argv[0]} 88.5")
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
    except Exception as e:
        print(f"Error loading config: {e}")
        sys.exit(1)

    # --- Validate Height Argument ---
    try:
        target_height_cm = float(sys.argv[1])
    except ValueError:
        print(f"Error: '{sys.argv[1]}' is not a valid height.")
        sys.exit(1)
    
    min_cm = config["height_limits"]["min_cm"]
    max_cm = config["height_limits"]["max_cm"]
    if not (min_cm <= target_height_cm <= max_cm):
        print(f"Error: Height {target_height_cm}cm is outside valid range ({min_cm}-{max_cm}).")
        sys.exit(1)
    
    # --- Convert Hex Commands to Bytes ---
    try:
        commands_hex = config["commands"]
        commands_bytes = {name: bytes.fromhex(cmd) for name, cmd in commands_hex.items()}
    except Exception as e:
        print(f"Error converting commands in config file: {e}")
        sys.exit(1)
        
    # --- Start Application ---
    context = DeskContext(target_height_cm)
    
    ble_thread = threading.Thread(
        target=run_ble_logic, 
        args=(context, config, commands_bytes)
    )
    ble_thread.start()
    
    print(f"Connecting to {config['device_address']} and reading initial height...")
    
    if not context.height_is_known_event.wait(timeout=10.0):
        print("Error: Could not connect or read initial height from desk.")
        print("Make sure it's on and not connected to another device.")
        context.quit_event.set()
        ble_thread.join()
        sys.exit(1)
    
    draw_ascii_ui(context, ble_thread)

if __name__ == "__main__":
    main()
    