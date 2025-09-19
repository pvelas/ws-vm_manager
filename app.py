import os
from flask import Flask, render_template, request, jsonify
import subprocess
import time
import logging
import shutil
import re
import glob

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

# --- Helper Functions ---
def is_workstation_gui_running():
    """Checks if the VMware Workstation GUI process is running on the host."""
    try:
        command = "/usr/bin/pgrep -f '^/usr/lib/vmware/bin/vmware$'"
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        return result.returncode == 0
    except Exception as e:
        logging.error(f"Error checking for VMware GUI process: {e}")
        return False

def timed_function(func):
    """Decorator to measure the execution time of a function."""
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result = func(*args, **kwargs)
        end_time = time.time()
        return result
    return wrapper

def find_vmx_files_with_walk(directories):
    vmx_files = {} 
    for lab_name, directory in directories.items():
        for root, dirs, files in os.walk(directory):
            for file in files:
                if file.lower().endswith(".vmx"):
                    if lab_name not in vmx_files: vmx_files[lab_name] = [] 
                    vmx_files[lab_name].append(os.path.join(root, file))  
    return vmx_files 

def clean_vm_locks(vmx_path):
    vm_dir = os.path.dirname(vmx_path)
    try:
        for item in os.listdir(vm_dir):
            if item.endswith('.lck'): shutil.rmtree(os.path.join(vm_dir, item))
    except Exception as e:
        logging.error(f"Error cleaning lock files in {vm_dir}: {e}")
        raise

def check_for_locks(vmx_path):
    vm_dir = os.path.dirname(vmx_path)
    try:
        for item in os.listdir(vm_dir):
            if item.endswith('.lck'): return True
    except FileNotFoundError: return False
    return False

def check_vm_logs_for_errors(vmx_path):
    """Scans all .log files in a VM's directory for critical error keywords."""
    vm_dir = os.path.dirname(vmx_path)
    error_keywords = ['unrecoverable', 'panic', 'coredump']
    found_lines = []
    
    try:
        log_files = glob.glob(os.path.join(vm_dir, 'vmware.log'))
        for log_file in log_files:
            with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line_lower = line.lower()
                    if any(keyword in line_lower for keyword in error_keywords):
                        found_lines.append(line.strip())
    except Exception as e:
        logging.error(f"Error reading log files for {vmx_path}: {e}")

    return {'count': len(found_lines), 'lines': found_lines}


def manage_vm(vmx_path, action, snapshot_name=None):
    command = []
    if action == "start": command = [VMRUN_PATH, "-T", "ws", action, vmx_path, "nogui"]
    elif action == "snapshot":
        if not snapshot_name: raise ValueError("Snapshot name required.")
        command = [VMRUN_PATH, "snapshot", vmx_path, snapshot_name]
    elif action == "revert":
        if not snapshot_name: raise ValueError("Snapshot name required.")
        command = [VMRUN_PATH, "revertToSnapshot", vmx_path, snapshot_name]
    else: command = [VMRUN_PATH, "-T", "ws", action, vmx_path]
    try:
        subprocess.run(command, capture_output=True, text=True, check=True)
        if action in ["snapshot", "revert"]:
            if vmx_path in vm_cache: del vm_cache[vmx_path]
    except subprocess.CalledProcessError as e:
        logging.error(f"Error on '{action}' for {vmx_path}: {e.stderr}")
        raise

def get_vm_snapshots(vmx_path):
    snapshots = []
    try:
        command = [VMRUN_PATH, "listSnapshots", vmx_path]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        lines = result.stdout.strip().splitlines()
        if len(lines) > 1: snapshots = [line.strip() for line in lines[1:]]
    except subprocess.CalledProcessError as e:
        logging.info(f"Could not list snapshots for {vmx_path}: {e.stderr.strip()}")
    return snapshots
    
def get_active_snapshot(vmx_path):
    vmsd_path = vmx_path.replace('.vmx', '.vmsd')
    if not os.path.exists(vmsd_path): return None
    current_snapshot_uid, snapshot_index, display_name, description = None, None, None, None
    try:
        with open(vmsd_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        uid_key_pattern = None
        for line in lines:
            line = line.strip()
            if line.startswith('snapshot.current'):
                match = re.search(r'snapshot\.current\s*=\s*"(\d+)"', line)
                if match: current_snapshot_uid = match.group(1); break
        if not current_snapshot_uid: return None 
        uid_key_pattern = re.compile(r'snapshot(\d+)\.uid\s*=\s*"' + re.escape(current_snapshot_uid) + r'"')
        for line in lines:
            match = uid_key_pattern.match(line.strip())
            if match: snapshot_index = match.group(1); break
        if not snapshot_index: return None
        name_key, desc_key = f'snapshot{snapshot_index}.displayName', f'snapshot{snapshot_index}.description'
        for line in lines:
            line = line.strip()
            if line.startswith(name_key):
                parts = line.split('=', 1); display_name = parts[1].strip().strip('"') if len(parts) == 2 else None
            elif line.startswith(desc_key):
                parts = line.split('=', 1); description = parts[1].strip().strip('"') if len(parts) == 2 else None
        return display_name or description
    except IOError as e:
        logging.warning(f"Could not read VMSD file {vmsd_path}: {e}")
    return None

# --- Core Logic ---
@timed_function
def get_all_vm_info(directories, force_refresh=False):
    if force_refresh: vm_cache.clear()
    result = subprocess.run([VMRUN_PATH, "list"], capture_output=True, text=True)
    running_vm_paths = [line.strip() for line in result.stdout.splitlines() if line.endswith(".vmx")]
    all_vms = []
    all_vmx_files_by_lab = find_vmx_files_with_walk(directories)
    flat_vmx_list = [vmx for vmx_list in all_vmx_files_by_lab.values() for vmx in vmx_list]

    for vmx in flat_vmx_list:
        current_time = time.time()
        is_running = vmx in running_vm_paths

        if not force_refresh and vmx in vm_cache and (current_time - vm_cache[vmx]['timestamp']) < CACHE_DURATION_SECONDS:
            static_data = vm_cache[vmx]['data']
            # If VM is running now, but wasn't when cached, we might need to re-check logs
            if is_running and not static_data.get('was_running_when_cached', False):
                 static_data['error_log_info'] = check_vm_logs_for_errors(vmx)
                 static_data['was_running_when_cached'] = True
                 vm_cache[vmx]['data'] = static_data # Update cache
        else:
            display_name, ethernet_devices = None, {}
            try:
                with open(vmx, 'r', encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        line_lower = line.strip().lower()
                        if line_lower.startswith("displayname"):
                            parts = line.split("=", 1)
                            if len(parts) == 2: display_name = parts[1].strip().strip('"')
                        elif line_lower.startswith("ethernet"):
                            parts = line.split('.', 1)
                            if len(parts) == 2:
                                adapter_id, key_value = parts[0], parts[1].split('=', 1)
                                if len(key_value) == 2:
                                    key, value = key_value[0].strip(), key_value[1].strip().strip('"')
                                    adapter_id_lower = adapter_id.lower()
                                    if adapter_id_lower not in ethernet_devices: ethernet_devices[adapter_id_lower] = {}
                                    key_lower = key.lower()
                                    if key_lower in ['vnet', 'generatedaddress']: ethernet_devices[adapter_id_lower][key_lower] = value
            except IOError as e:
                logging.error(f"Could not read file {vmx}: {e}")

            vm_name = display_name if display_name else os.path.basename(vmx).split(".")[0]
            
            processed_devices = {}
            for adapter_id, properties in ethernet_devices.items():
                processed_devices[adapter_id] = { 'vnet': properties.get('vnet'), 'generatedAddress': properties.get('generatedaddress')}

            static_data = {
                "title": vm_name,
                "ethernet_devices": processed_devices,
                "snapshots": get_vm_snapshots(vmx),
                "active_snapshot": get_active_snapshot(vmx),
                "error_log_info": check_vm_logs_for_errors(vmx) if is_running else {'count': 0, 'lines': []},
                "was_running_when_cached": is_running
            }
            vm_cache[vmx] = {'data': static_data, 'timestamp': current_time}

        has_locks = False if is_running else check_for_locks(vmx)

        ip_address = "N/A"
        if is_running:
            command = [VMRUN_PATH, "-T", "ws", "getGuestIPAddress", vmx]
            ip_result = subprocess.run(command, capture_output=True, text=True)
            if ip_result.returncode == 0 and ip_result.stdout.strip(): ip_address = ip_result.stdout.strip()
        
        details = [f"IPv4: {ip_address}"]
        for adapter_id in sorted(static_data['ethernet_devices'].keys()):
            device = static_data['ethernet_devices'][adapter_id]
            mac, net_path = device.get('generatedAddress'), device.get('vnet')
            net_name = os.path.basename(net_path) if net_path else 'N/A'
            if mac: details.append(f"MAC {net_name}: {mac}")
        
        lab_name_for_vm = "Unknown";
        for lab, vmx_paths in all_vmx_files_by_lab.items():
            if vmx in vmx_paths: lab_name_for_vm = lab; break

        # Use cached error log info if VM is now offline
        error_info_to_display = static_data.get('error_log_info', {'count': 0, 'lines': []})

        all_vms.append({
            "lab_name": lab_name_for_vm, "title": static_data['title'], "complete": is_running, "has_locks": has_locks,
            "vmx_path": vmx, "snapshots": static_data['snapshots'], "active_snapshot": static_data.get('active_snapshot'),
            "details": details, "error_log_info": error_info_to_display
        })
        
    return sorted(all_vms, key=lambda vm: (vm['lab_name'], vm['title']))

# --- Flask Routes ---
@app.route("/", methods=["GET"])
def index():
    full_refresh = request.args.get('full_refresh', 'false').lower() == 'true'
    all_vms = get_all_vm_info(VM_DIRECTORY, force_refresh=full_refresh)
    is_gui_running = is_workstation_gui_running()
    vm_data_by_lab = {}
    for vm in all_vms:
        lab = vm['lab_name']
        if lab not in vm_data_by_lab: vm_data_by_lab[lab] = []
        vm_data_by_lab[lab].append(vm)
    return render_template("index.html", vm_data_by_lab=vm_data_by_lab, is_gui_running=is_gui_running)

@app.route("/api/vm/logs", methods=['POST'])
def api_vm_logs():
    data = request.get_json()
    vmx_path = data.get('vmx_path')
    if not vmx_path:
        return jsonify({'status': 'error', 'message': 'Missing vmx_path parameter'}), 400
    try:
        # We can re-run the check here to ensure we get the absolute latest logs
        log_data = check_vm_logs_for_errors(vmx_path)
        return jsonify({'status': 'success', 'logs': log_data['lines']})
    except Exception as e:
        logging.error(f"API Error on getting logs for {vmx_path}: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route("/api/vm/action", methods=['POST'])
def api_vm_action():
    data = request.get_json(); vmx_path = data.get('vmx_path')
    action = data.get('action'); snapshot_name = data.get('snapshot_name')
    if not vmx_path or not action: return jsonify({'status': 'error', 'message': 'Missing parameters'}), 400
    try:
        if action == "clean_locks": clean_vm_locks(vmx_path)
        else: manage_vm(vmx_path, action, snapshot_name=snapshot_name)
        return jsonify({'status': 'success', 'message': f'Action {action} completed successfully.'})
    except Exception as e:
        logging.error(f"API Error on '{action}' for {vmx_path}: {e}")
        error_message = str(e.stderr) if hasattr(e, 'stderr') and e.stderr else str(e)
        return jsonify({'status': 'error', 'message': error_message}), 500

# --- Main ---
if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True)

