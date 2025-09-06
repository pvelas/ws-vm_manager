import os
from flask import Flask, render_template, request, redirect, url_for, jsonify
import subprocess
from datetime import datetime
import time
import logging
from flask import current_app

app = Flask(__name__)

VMRUN_PATH = '/usr/bin/vmrun'
logging.basicConfig(filename='vm_manager.log', level=logging.INFO)

VM_DIRECTORY = {
    "99_infra_red_net": "/home/velo/vmware/99_infra_red_net/",
    "99_red_net": "/home/velo/vmware/99_red_net/"
}

def timed_function(func):
    """Decorator to measure the execution time of a function."""
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result = func(*args, **kwargs)
        end_time = time.time()
        execution_time = end_time - start_time
        print(f"Function {func.__name__} took {execution_time:.2f} seconds to execute")
        return result
    return wrapper

@timed_function
def find_vmx_files_with_walk(directories):
    vmx_files = {} 
    for lab_name, directory in directories.items():
        for root, dirs, files in os.walk(directory):
            for file in files:
                if file.lower().endswith(".vmx"):
                    if lab_name not in vmx_files:
                        vmx_files[lab_name] = [] 
                    vmx_files[lab_name].append(os.path.join(root, file))  
    return vmx_files 

@timed_function
def manage_vm(vmx_path, action, snapshot_name=None):
    """Starts, stops, restarts, or snapshots a VM."""
    command = []
    if action == "start":
        command = [VMRUN_PATH, "-T", "ws", action, vmx_path, "nogui"]
    elif action == "snapshot":
        if not snapshot_name:
            logging.error("Snapshot action called without a snapshot name.")
            raise ValueError("Snapshot name is required.")
        command = [VMRUN_PATH, "snapshot", vmx_path, snapshot_name]
    else:
        command = [VMRUN_PATH, "-T", "ws", action, vmx_path]
    
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        logging.info(f"Successfully executed '{action}' on {vmx_path}. Output: {result.stdout}")

    except subprocess.CalledProcessError as e:
        logging.error(f"Error executing vmrun '{action}' on {vmx_path} (return code {e.returncode}): {e.stderr}")
        raise
    
    if 'result' in locals() and result.returncode != 0:
        print("Error:", result.stderr, flush=True)
    else:
        print("Success:", "Command executed.", flush=True)


@timed_function
def get_vm_snapshots(vmx_path):
    """Gets the list of snapshots for a single VM."""
    snapshots = []
    try:
        command = [VMRUN_PATH, "listSnapshots", vmx_path]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        lines = result.stdout.strip().splitlines()
        if len(lines) > 1:
            snapshots = [line.strip() for line in lines[1:]]
    except subprocess.CalledProcessError as e:
        logging.info(f"Could not list snapshots for {vmx_path} (maybe none exist): {e.stderr.strip()}")
    except FileNotFoundError:
        logging.error(f"vmrun command not found at path: {VMRUN_PATH}")
    return snapshots

@timed_function
def get_all_vm_info(directories):
    """Gets info for all VMs, including running status, MAC, IP, and snapshots."""
    result = subprocess.run([VMRUN_PATH, "list"], capture_output=True, text=True)
    running_vm_paths = [line.strip() for line in result.stdout.splitlines() if line.endswith(".vmx")]
    
    vm_info = {}
    for lab_name, vmx_list in find_vmx_files_with_walk(directories).items():
        for vmx in vmx_list:
            display_name = None
            try:
                with open(vmx, 'r', encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        if line.strip().startswith("displayName"):
                            display_name = line.split("=")[1].strip().strip('"')
                            break
            except IOError as e:
                logging.error(f"Could not read file {vmx}: {e}")

            vm_name = display_name if display_name else os.path.basename(vmx).split(".")[0]
            is_running = vmx in running_vm_paths

            details = []
            
            # Get IP Address if running
            ip_address = "N/A"
            if is_running:
                command = [VMRUN_PATH, "-T", "ws", "getGuestIPAddress", vmx]
                ip_result = subprocess.run(command, capture_output=True, text=True)
                if ip_result.returncode == 0 and ip_result.stdout.strip():
                    ip_address = ip_result.stdout.strip()
                else:
                    logging.warning(f"Could not retrieve IP for running VM {vmx}: {ip_result.stderr.strip()}")
            details.append(f"IPv4: {ip_address}")

            # Get MAC addresses with their corresponding vmnet interfaces
            ethernet_devices = {}
            try:
                with open(vmx, 'r', encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("ethernet"):
                            parts = line.split('.', 1)
                            if len(parts) == 2:
                                adapter_id = parts[0]
                                key_value = parts[1].split('=', 1)
                                if len(key_value) == 2:
                                    key = key_value[0].strip()
                                    value = key_value[1].strip().strip('"')
                                    if adapter_id not in ethernet_devices:
                                        ethernet_devices[adapter_id] = {}
                                    if key in ['vnet', 'generatedAddress']:
                                        ethernet_devices[adapter_id][key] = value
            except IOError as e:
                 logging.error(f"Could not read file {vmx} for MAC details: {e}")

            for adapter_id in sorted(ethernet_devices.keys()):
                device = ethernet_devices[adapter_id]
                mac = device.get('generatedAddress')
                net_name_raw = device.get('vnet', 'N/A')
                net_name = os.path.basename(net_name_raw) if net_name_raw != 'N/A' else 'N/A'
                if mac:
                    details.append(f"MAC {net_name}: {mac}")

            snapshots = get_vm_snapshots(vmx)

            vm_info[(lab_name, vm_name)] = {
                "title": vm_name,
                "complete": is_running,
                "vmx_path": vmx,
                "snapshots": snapshots,
                "details": details
            }
    return vm_info

# --- Flask Routes ---
@app.route("/", methods=["GET"])
def index():
    vm_info = get_all_vm_info(VM_DIRECTORY)
    vm_data_by_lab = {}
    for (lab_name, vm_name), vm_data in vm_info.items():
        if lab_name not in vm_data_by_lab:
            vm_data_by_lab[lab_name] = []
        vm_data_by_lab[lab_name].append(vm_data)

    # Sort labs and VMs within labs
    sorted_labs = sorted(vm_data_by_lab.keys())
    sorted_vm_data_by_lab = {lab: sorted(vm_data_by_lab[lab], key=lambda vm: vm["title"]) for lab in sorted_labs}

    return render_template("index.html", vm_data_by_lab=sorted_vm_data_by_lab)


@app.route("/api/vm/action", methods=['POST'])
def api_vm_action():
    data = request.get_json()
    vmx_path = data.get('vmx_path')
    action = data.get('action')
    snapshot_name = data.get('snapshot_name')
    
    if not vmx_path or not action:
        return jsonify({'status': 'error', 'message': 'Missing parameters'}), 400

    try:
        manage_vm(vmx_path, action, snapshot_name=snapshot_name)
        return jsonify({'status': 'success', 'message': f'Action {action} initiated.'})
    except Exception as e:
        logging.error(f"API Error on action '{action}' for {vmx_path}: {e}")
        # Pass the specific error message from stderr if available
        error_message = str(e.stderr) if hasattr(e, 'stderr') else str(e)
        return jsonify({'status': 'error', 'message': error_message}), 500

# --- Main ---
if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)

