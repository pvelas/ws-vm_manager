import os
from flask import Flask, render_template, request, jsonify
import subprocess
import time
import logging
import shutil
import re
import glob
from functools import wraps

app = Flask(__name__)

# --- Configuration ---
VMRUN_PATH = '/usr/bin/vmrun'
VM_DIRECTORY = {
    "99_infra_red_net": "/home/velo/vmware/99_infra_red_net/",
    "99_red_net": "/home/velo/vmware/99_red_net/"
}
CACHE_DURATION_SECONDS = 300
logging.basicConfig(level=logging.INFO)

# --- In-Memory Cache ---
vm_cache = {}

# --- Helper Functions ---
def timed_function(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result = f(*args, **kwargs)
        end_time = time.time()
        logging.info(f"Function {f.__name__} took {end_time - start_time:.2f} seconds")
        return result
    return wrapper

def run_command(command):
    try:
        result = subprocess.run(command, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    except subprocess.CalledProcessError as e:
        return e.stdout.strip(), e.stderr.strip(), e.returncode

def is_workstation_gui_running():
    """Checks if the VMware Workstation GUI process is running on the host."""
    try:
        # Use a precise pattern to match only the main GUI process
        command = "/usr/bin/pgrep -f '^/usr/lib/vmware/bin/vmware$'"
        stdout, _, returncode = run_command(command)
        return returncode == 0 and stdout.strip() != ""
    except Exception:
        return False
        
def get_active_snapshot(vmx_path):
    vm_dir = os.path.dirname(vmx_path)
    vmsd_path = vmx_path.replace('.vmx', '.vmsd')
    if not os.path.exists(vmsd_path):
        return None

    try:
        with open(vmsd_path, 'r', errors='ignore') as f:
            lines = f.readlines()

        snapshot_uid_map = {}
        for line in lines:
            match = re.match(r'snapshot(\d+)\.uid\s*=\s*"(\d+)"', line.strip())
            if match:
                index, uid = match.groups()
                snapshot_uid_map[uid] = index
        
        current_uid = None
        for line in lines:
            if line.strip().startswith('snapshot.current ='):
                match = re.search(r'"(\d+)"', line)
                if match:
                    current_uid = match.group(1)
                    break
        
        if not current_uid or current_uid not in snapshot_uid_map:
            return None
            
        current_index = snapshot_uid_map[current_uid]
        
        display_name_key = f'snapshot{current_index}.displayName'
        description_key = f'snapshot{current_index}.description'
        
        snapshot_name = None
        for line in lines:
            if line.strip().startswith(display_name_key):
                match = re.search(r'"(.*?)"', line)
                if match:
                    snapshot_name = match.group(1)
                    break # Prefer display name
        
        if not snapshot_name:
            for line in lines:
                 if line.strip().startswith(description_key):
                    match = re.search(r'"(.*?)"', line)
                    if match:
                        snapshot_name = match.group(1)
                        break
        
        return snapshot_name
    except Exception as e:
        logging.error(f"Error parsing VMSD file {vmsd_path}: {e}")
        return None

def check_for_locks(vm_dir):
    return any(name.endswith('.lck') for name in os.listdir(vm_dir))

def clean_vm_locks(vm_dir):
    for item in os.listdir(vm_dir):
        if item.endswith('.lck'):
            path = os.path.join(vm_dir, item)
            shutil.rmtree(path)
            logging.info(f"Removed lock directory: {path}")

def parse_vmx_details(vmx_path):
    details = []
    try:
        with open(vmx_path, 'r', errors='ignore') as f:
            lines = [line.strip().lower() for line in f.readlines()]
        
        nics = {}
        for line in lines:
            match = re.match(r'^(ethernet(\d+))\.(.+?)\s*=\s*"(.+?)"', line)
            if match:
                key, nic_num, prop, value = match.groups()
                if nic_num not in nics:
                    nics[nic_num] = {}
                nics[nic_num][prop] = value
        
        for num, data in sorted(nics.items()):
            mac = data.get('generatedaddress')
            vnet = data.get('vnet', 'N/A')
            if vnet != 'N/A':
                vnet = os.path.basename(vnet)
            if mac:
                details.append(f"MAC {vnet}: {mac.upper()}")
                
    except Exception as e:
        logging.error(f"Could not parse VMX {vmx_path}: {e}")
    return details

def check_vm_logs_for_errors(vm_dir):
    error_patterns = re.compile(r'unrecoverable|panic|coredump', re.IGNORECASE)
    error_lines = []
    
    log_file = os.path.join(vm_dir, 'vmware.log')
    if not os.path.exists(log_file):
        return {'count': 0, 'lines': []}
        
    try:
        with open(log_file, 'r', errors='ignore') as f:
            for line in f:
                if error_patterns.search(line):
                    error_lines.append(line.strip())
    except Exception as e:
        logging.error(f"Error reading log file {log_file}: {e}")

    return {'count': len(error_lines), 'lines': error_lines}

@timed_function
def get_all_vm_info(force_refresh=False):
    all_vms = []
    now = time.time()
    
    stdout, _, _ = run_command(f'{VMRUN_PATH} list')
    running_vms = [path.strip() for path in stdout.splitlines() if path.strip().endswith('.vmx')]

    for lab_name, directory in VM_DIRECTORY.items():
        if not os.path.isdir(directory): continue
        for vmx_file in glob.glob(os.path.join(directory, '**', '*.vmx'), recursive=True):
            vmx_path = os.path.abspath(vmx_file)
            vm_dir = os.path.dirname(vmx_path)
            
            cached_data = vm_cache.get(vmx_path)
            is_cached = cached_data and (now - cached_data.get('timestamp', 0)) < CACHE_DURATION_SECONDS
            
            if force_refresh or not is_cached:
                title = os.path.splitext(os.path.basename(vmx_path))[0]
                try:
                    with open(vmx_path, 'r', errors='ignore') as f:
                        for line in f:
                            if 'displayname' in line.lower():
                                title_match = re.search(r'"(.*?)"', line)
                                if title_match: title = title_match.group(1)
                                break
                except Exception: pass
                
                stdout, _, _ = run_command(f'{VMRUN_PATH} listSnapshots "{vmx_path}"')
                snapshots = [s for s in stdout.splitlines() if not s.lower().startswith('total snapshots:')]

                cached_data = {
                    'title': title,
                    'snapshots': snapshots,
                    'details': parse_vmx_details(vmx_path),
                    'timestamp': now
                }
            
            is_running = vmx_path in running_vms
            
            # Update dynamic data in cache, especially log info for running VMs
            if is_running:
                cached_data['error_log_info'] = check_vm_logs_for_errors(vm_dir)
            
            vm_cache[vmx_path] = cached_data

            all_vms.append({
                'vmx_path': vmx_path,
                'lab': lab_name,
                'title': cached_data['title'],
                'complete': is_running,
                'details': cached_data['details'],
                'snapshots': cached_data.get('snapshots', []),
                'active_snapshot': get_active_snapshot(vmx_path),
                'has_locks': check_for_locks(vm_dir) if not is_running else False,
                'error_log_info': cached_data.get('error_log_info')
            })

    vms_by_lab = {}
    for vm in all_vms:
        if vm['lab'] not in vms_by_lab: vms_by_lab[vm['lab']] = []
        vms_by_lab[vm['lab']].append(vm)
    
    # Also return a flat list for compact view
    all_vms.sort(key=lambda x: x['title'])
    return vms_by_lab, all_vms

# --- Routes ---
@app.route('/')
def index():
    force_refresh = request.args.get('full_refresh', 'false').lower() == 'true'
    vm_data_by_lab, all_vms_sorted = get_all_vm_info(force_refresh)
    return render_template('index.html', 
                           vm_data_by_lab=vm_data_by_lab, 
                           all_vms=all_vms_sorted,
                           is_gui_running=is_workstation_gui_running())

@app.route('/api/vm/action', methods=['POST'])
def manage_vm():
    data = request.json
    vmx_path = data.get('vmx_path')
    action = data.get('action')
    snapshot_name = data.get('snapshot_name')
    
    if not vmx_path or not os.path.exists(vmx_path):
        return jsonify({'status': 'error', 'message': 'VMX path not found'}), 400

    vm_dir = os.path.dirname(vmx_path)
    command = None

    if action in ['start', 'stop', 'restart']:
        command = f'{VMRUN_PATH} {action} "{vmx_path}" nogui'
    elif action == 'snapshot' and snapshot_name:
        command = f'{VMRUN_PATH} snapshot "{vmx_path}" "{snapshot_name}"'
    elif action == 'revert' and snapshot_name:
        command = f'{VMRUN_PATH} revertToSnapshot "{vmx_path}" "{snapshot_name}"'
    elif action == 'clean_locks':
        clean_vm_locks(vm_dir)
        if vmx_path in vm_cache: del vm_cache[vmx_path] # Force refresh for this VM
        return jsonify({'status': 'success', 'message': 'Locks cleaned'})

    if command:
        _, stderr, returncode = run_command(command)
        if returncode == 0:
            if vmx_path in vm_cache: del vm_cache[vmx_path] # Force refresh for this VM
            return jsonify({'status': 'success'})
        else:
            return jsonify({'status': 'error', 'message': stderr or "Unknown error"}), 500
    
    return jsonify({'status': 'error', 'message': 'Invalid action'}), 400

@app.route('/api/vm/logs', methods=['POST'])
def get_vm_logs():
    data = request.json
    vmx_path = data.get('vmx_path')
    if not vmx_path or not os.path.exists(vmx_path):
        return jsonify({'status': 'error', 'message': 'VMX path not found'}), 400
    
    cached_data = vm_cache.get(vmx_path)
    if cached_data and 'error_log_info' in cached_data:
        return jsonify({'status': 'success', 'logs': cached_data['error_log_info']['lines']})
    else:
        # Fallback to a live check if not in cache (e.g., after a full refresh)
        vm_dir = os.path.dirname(vmx_path)
        log_info = check_vm_logs_for_errors(vm_dir)
        return jsonify({'status': 'success', 'logs': log_info['lines']})

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)

