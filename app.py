import os
from flask import Flask, render_template, request, redirect, url_for
import subprocess
from datetime import datetime
import time
import logging
from flask import current_app

app = Flask(__name__)

logging.basicConfig(filename='vm_manager.log', level=logging.INFO)

VM_DIRECTORY = {
    "20_lab_vulnhub": "/home/username/vmware/20_lab_vulnhub/",
    "20_lab_vulnhub_2004-2009": "/home/username/vmware/20_lab_vulnhub_2004-2009/"
}




# --- Your Existing Function ---

def timed_function(func):
    """Decorator to measure the execution time of a function."""
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result = func(*args, **kwargs)
        end_time = time.time()
        execution_time = end_time - start_time
        print(f"Function {func.__name__} took {execution_time:.2f} seconds to execute")  # Debug message
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


# --- New Functions ---
@timed_function
def get_vm_status(vm_info, vmx_path):
    """Gets the running status of a VM using the cached vm_info."""
    vm_name = os.path.basename(vmx_path).split(".")[0]
    lab_name = vmx_path.split('/')[-2] # Extract lab name from vmx_path
    vm_data = vm_info.get((lab_name, vm_name))
    return "Running" if vm_data["complete"] else "Stopped"


@timed_function
def get_vm_network_details(vmx_path):
    """Gets all MAC addresses and the first available IPv4 address (or N/A) using vmrun."""

    mac_addresses = []
    ip_address = "N/A"

    # Get MAC addresses from .vmx file, avoiding duplicates and offset lines
    seen_macs = set()
    with open(vmx_path, 'r') as f:
        for line in f:
            if line.startswith("ethernet") and "generatedAddress" in line:
                mac = line.split("=")[1].strip().strip('"')
                if mac not in seen_macs and not mac.isdigit():  # Filter out duplicates and offsets
                    mac_addresses.append(mac)
                    seen_macs.add(mac)

    if not mac_addresses:
        return ["No MAC addresses found in .vmx file"]

    # Get first available IPv4 address using getGuestIPAddresses
    status = get_vm_status(vmx_path)
    if status == "Running":
        command = ["vmrun", "-T", "ws", "getGuestIPAddresses", vmx_path]
        result = subprocess.run(command, capture_output=True, text=True)

        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("ethernet"):
                    ip_address = line.split()[1].strip()  # Get the first valid IP
                    break  # Stop after finding one

    mac_info = ", ".join(f"MAC: {mac}" for mac in mac_addresses)
    return [f"IPv4: {ip_address}"] + mac_addresses   # Combine details (IP first, then MACs)


@timed_function
def manage_vm(vmx_path, action):
    """Starts, stops, or restarts a VM using vmrun in headless mode."""

    if action == "start":
        command = ["vmrun", "-T", "ws", action, vmx_path, "nogui"]  # Always add 'nogui' for start
    else:
        command = ["vmrun", "-T", "ws", action, vmx_path]           # No 'nogui' needed for stop/reset

    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        logging.error(f"Error executing vmrun (return code {e.returncode}): {e.stderr}")
        raise

    # Print output for debugging in the console (optional)
    if result.returncode != 0:
        print("Error:", result.stderr, flush=True)
    else:
        print("Success:", result.stdout, flush=True)



@timed_function
def get_all_vm_info(directories):
    """Gets info for all VMs, including running status, MAC, and IP (if running)."""

    result = subprocess.run(["vmrun", "list"], capture_output=True, text=True)
    running_vm_files = [os.path.basename(line.strip()) for line in result.stdout.splitlines() if line.endswith(".vmx")]

    vm_info = {}

    for lab_name, vmx_list in find_vmx_files_with_walk(directories).items():
        for vmx in vmx_list:
            vm_name = os.path.basename(vmx).split(".")[0]
            is_running = os.path.basename(vmx) in running_vm_files

            mac_addresses = []
            ip_addresses = []  
            details = []

            # Get MAC addresses from .vmx file, avoiding duplicates and offset lines
            seen_macs = set()
            with open(vmx, 'r') as f:
                for line in f:
                    if line.startswith("ethernet") and "generatedAddress" in line:
                        mac = line.split("=")[1].strip().strip('"')
                        if mac not in seen_macs and not mac.isdigit():  # Filter out duplicates and offsets
                            mac_addresses.append(mac)
                            seen_macs.add(mac)

            if not mac_addresses:
                return ["No MAC addresses found in .vmx file"]

            # Get IPv4 addresses using getGuestIPAddress (singular) only if VM is running
            if is_running:
                command = ["vmrun", "-T", "ws", "getGuestIPAddress", vmx] # <--- corrected this line
                result = subprocess.run(command, capture_output=True, text=True)

                if result.returncode == 0:
                    # Assuming the first valid IP address found for an interface is the correct one
                    ip_address = result.stdout.splitlines()[0].strip()
                    ip_addresses.append(ip_address)
                else:
                    logging.error(f"Error retrieving IP for {vmx}: {result.stderr}")
                    ip_addresses = ["Error retrieving IP"] 

            # If the VM is not running, add "N/A" for IPv4
            if not ip_addresses:
                ip_addresses = ["N/A"] 

            # Combine MAC and IP details (only the first IP address)
            if ip_addresses:
                details.append(f"IPv4: {ip_addresses[0]}") 
            details.extend([f"MAC: {mac}" for mac in mac_addresses]) 

            vm_info[(lab_name, vm_name)] = {
                "title": vm_name,
                "complete": is_running,
                "vmx_path": vmx,
                "details": details
            }
            current_app.logger.debug(f"VM info for {vm_name}: {vm_info[(lab_name, vm_name)]}") # Additional debugging
            current_app.logger.debug(f"Result of vmrun getGuestIPAddresses for {vm_name}: {result}")
    current_app.logger.debug(f"get_all_vm_info function finished, returning data: {vm_info}")
    return vm_info

# --- Flask Routes ---
@app.route("/", methods=["GET", "POST"])
def index():
    vmx_files = find_vmx_files_with_walk(VM_DIRECTORY)
    vm_info = get_all_vm_info(VM_DIRECTORY)  # Fetch VM info before each request
    vm_data_by_lab = {}

    if request.method == 'POST':
        vmx_path = request.form['vmx_path']
        action = request.form['action']

        # Fix for get_vm_status call:
        vm_name = os.path.basename(vmx_path).split(".")[0]
        lab_name = vmx_path.split('/')[-2]  

        # Refreshed the VM status before performing the action
        manage_vm(vmx_path, action) 
        vm_info = get_all_vm_info(VM_DIRECTORY)  # Refresh VM info after the action
        # Update the status based on the new vm_info

        return redirect(url_for("index"))  
    # Prepare data for the template, grouping by lab and sorting
    for (lab_name, vm_name), vm_data in vm_info.items():
        if lab_name not in vm_data_by_lab:
            vm_data_by_lab[lab_name] = []
        vm_data_by_lab[lab_name].append(vm_data)

    # Sort labs and VMs within each lab
    sorted_labs = sorted(vm_data_by_lab.keys())
    for lab_name in sorted_labs:
        vm_data_by_lab[lab_name].sort(key=lambda vm: vm["title"])

    print("VM data:", vm_data_by_lab) 
    return render_template("index.html", vm_data_by_lab=vm_data_by_lab)





# --- Main ---
if __name__ == "__main__":
    app.run(debug=True)

