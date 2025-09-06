import os
from flask import Flask, render_template, request, jsonify
import subprocess
import time
import logging
import shutil

app = Flask(__name__)

# --- Configuration ---
VMRUN_PATH = '/usr/bin/vmrun'
VM_DIRECTORY = {
    "99_infra_red_net": "/home/velo/vmware/99_infra_red_net/",
    "99_red_net": "/home/velo/vmware/99_red_net/"
}
CACHE_DURATION_SECONDS = 300
logging.basicConfig(filename='vm_manager.log', level=logging.INFO)

# --- In-Memory Cache ---
vm_cache = {}

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

def clean_vm_locks(vmx_path):
    """Finds and removes .lck directories in the VM's folder."""
    vm_dir = os.path.dirname(vmx_path)
    logging.info(f"Attempting to clean lock files in directory: {vm_dir}")
    try:
        for item in os.listdir(vm_dir):
            if item.endswith('.lck'):
                lock_path = os.path.join(vm_dir, item)
                shutil.rmtree(lock_path)
                logging.info(f"Successfully removed lock directory: {lock_path}")
    except Exception as e:
        logging.error(f"Error cleaning lock files in {vm_dir}: {e}")
        raise

def check_for_locks(vmx_path):
    """Checks if any .lck files/directories exist for a given VM."""
    vm_dir = os.path.dirname(vmx_path)
    try:
        for item in os.listdir(vm_dir):
            if item.endswith('.lck'):
                return True
    except FileNotFoundError:
        return False
    return False

def manage_vm(vmx_path, action, snapshot_name=None):
    """Starts, stops, restarts, or snapshots a VM."""
    command = []
    if action == "start":
        command = [VMRUN_PATH, "-T", "ws", action, vmx_path, "nogui"]
    elif action == "snapshot":
        if not snapshot_name:
            raise ValueError("Snapshot name is required.")
        command = [VMRUN_PATH, "snapshot", vmx_path, snapshot_name]
    else:
        command = [VMRUN_PATH, "-T", "ws", action, vmx_path]
    
    try:
        subprocess.run(command, capture_output=True, text=True, check=True)
        logging.info(f"Successfully executed '{action}' on {vmx_path}.")
        if action == "snapshot":
            if vmx_path in vm_cache:
                del vm_cache[vmx_path]
    except subprocess.CalledProcessError as e:
        logging.error(f"Error executing vmrun '{action}' on {vmx_path}: {e.stderr}")
        raise

@timed_function
def get_all_vm_info(directories, force_refresh=False):
    """
    Gets info for all VMs, using a cache unless force_refresh is True.
    """
    if force_refresh:
        print("--- FULL REFRESH INVOKED: Bypassing cache. ---")
        
    result = subprocess.run([VMRUN_PATH, "list"], capture_output=True, text=True)
    running_vm_paths = [line.strip() for line in result.stdout.splitlines() if line.endswith(".vmx")]
    
    vm_info = {}
    all_vmx_files = find_vmx_files_with_walk(directories)
    
    for lab_name, vmx_list in all_vmx_files.items():
        for vmx in vmx_list:
            current_time = time.time()
            
            if not force_refresh and vmx in vm_cache and (current_time - vm_cache[vmx]['timestamp']) < CACHE_DURATION_SECONDS:
                static_data = vm_cache[vmx]['data']
                print(f"Cache HIT for {vmx}")
            else:
                if force_refresh:
                     print(f"FORCE REFRESH for {vmx}")
                else:
                     print(f"Cache MISS for {vmx}")

                display_name = None
                ethernet_devices = {}
                try:
                    with open(vmx, 'r', encoding='utf-8', errors='ignore') as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith("displayName"):
                                display_name = line.split("=")[1].strip().strip('"')
                            elif line.startswith("ethernet"):
                                parts = line.split('.', 1)
                                if len(parts) == 2:
                                    adapter_id = parts[0]
                                    key_value = parts[1].split('=', 1)
                                    if len(key_value) == 2:
                                        key, value = key_value[0].strip(), key_value[1].strip().strip('"')
                                        if adapter_id not in ethernet_devices:
                                            ethernet_devices[adapter_id] = {}
                                        if key in ['vnet', 'generatedAddress']:
                                            ethernet_devices[adapter_id][key] = value
                except IOError as e:
                    logging.error(f"Could not read file {vmx}: {e}")

                vm_name = display_name if display_name else os.path.basename(vmx).split(".")[0]
                snapshots = get_vm_snapshots(vmx)

                static_data = {
                    "title": vm_name,
                    "ethernet_devices": ethernet_devices,
                    "snapshots": snapshots
                }
                vm_cache[vmx] = {'data': static_data, 'timestamp': current_time}

            is_running = vmx in running_vm_paths
            has_locks = False if is_running else check_for_locks(vmx)

            ip_address = "N/A"
            if is_running:
                command = [VMRUN_PATH, "-T", "ws", "getGuestIPAddress", vmx]
                ip_result = subprocess.run(command, capture_output=True, text=True)
                if ip_result.returncode == 0 and ip_result.stdout.strip():
                    ip_address = ip_result.stdout.strip()
            
            details = [f"IPv4: {ip_address}"]
            for adapter_id in sorted(static_data['ethernet_devices'].keys()):
                device = static_data['ethernet_devices'][adapter_id]
                mac = device.get('generatedAddress')
                net_name_raw = device.get('vnet', 'N/A')
                net_name = os.path.basename(net_name_raw) if net_name_raw != 'N/A' else 'N/A'
                if mac:
                    details.append(f"MAC {net_name}: {mac}")
            
            vm_info[(lab_name, static_data['title'])] = {
                "title": static_data['title'],
                "complete": is_running,
                "has_locks": has_locks,
                "vmx_path": vmx,
                "snapshots": static_data['snapshots'],
                "details": details
            }
    return vm_info

def get_vm_snapshots(vmx_path):
    snapshots = []
    try:
        command = [VMRUN_PATH, "listSnapshots", vmx_path]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        lines = result.stdout.strip().splitlines()
        if len(lines) > 1:
            snapshots = [line.strip() for line in lines[1:]]
    except subprocess.CalledProcessError as e:
        logging.info(f"Could not list snapshots for {vmx_path}: {e.stderr.strip()}")
    return snapshots

# --- Flask Routes ---
@app.route("/", methods=["GET"])
def index():
    full_refresh = request.args.get('full_refresh', 'false').lower() == 'true'
    vm_info = get_all_vm_info(VM_DIRECTORY, force_refresh=full_refresh)
    
    vm_data_by_lab = {}
    for (lab_name, vm_name), vm_data in vm_info.items():
        if lab_name not in vm_data_by_lab:
            vm_data_by_lab[lab_name] = []
        vm_data_by_lab[lab_name].append(vm_data)

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
        if action == "clean_locks":
            clean_vm_locks(vmx_path)
        else:
            manage_vm(vmx_path, action, snapshot_name=snapshot_name)
        
        return jsonify({'status': 'success', 'message': f'Action {action} completed.'})
    except Exception as e:
        logging.error(f"API Error on action '{action}' for {vmx_path}: {e}")
        error_message = str(e.stderr) if hasattr(e, 'stderr') else str(e)
        return jsonify({'status': 'error', 'message': error_message}), 500

# --- Main ---
if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)

