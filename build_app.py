import subprocess
import sys
import os
import shutil
import requests
import atexit
from pathlib import Path
import zipfile
import time

# --- Configuration ---
PROJECT_ROOT = Path(__file__).resolve().parent
CONSUMET_API_DIR = PROJECT_ROOT / "api.consumet.org"

# Using 'essentials' build for fewer DLLs, usually just ffmpeg.exe and a few core DLLs
FFMPEG_DOWNLOAD_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FFMPEG_ARCHIVE_NAME = "ffmpeg-release-essentials.zip"
FFMPEG_EXTRACT_TEMP_DIR = PROJECT_ROOT / "ffmpeg_temp_extract"

EXE_NAME = "BingeWatch"
PYTHON_VENV_DIR = PROJECT_ROOT / "venv" # Default virtual environment folder name

# Global to store Consumet process (if we start it in the script)
CONSUMET_PROCESS = None

# --- Helper Functions ---

def run_command(cmd, cwd=None, shell=False, check=True, capture_output=True, env=None): # << CHANGED capture_output=True
    """Executes a shell command and captures output on failure."""
    cmd_str = ' '.join(cmd) if isinstance(cmd, list) else cmd
    print(f"\n>>> Running: {cmd_str} (in {cwd if cwd else 'current directory'})")
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            shell=shell,
            check=check,
            capture_output=capture_output, # << Set to True
            text=True,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' and not shell else 0
        )
        # Only print output if check=False or command succeeded, and capture_output was True
        if capture_output and not check:
             print("STDOUT:\n", result.stdout)
             if result.stderr:
                 print("STDERR:\n", result.stderr)
        elif capture_output and check and result.returncode == 0:
             # Optional: Print success output if you want to see it
             # print("STDOUT:\n", result.stdout)
             # if result.stderr:
             #     print("STDERR:\n", result.stderr)
             pass # Don't print large success output by default

        return result
    except subprocess.CalledProcessError as e:
        print(f"!!! ERROR: Command failed with exit code {e.returncode}")
        # --- ADDED: Print captured output on failure ---
        if capture_output:
            if e.stdout: print("STDOUT:\n", e.stdout)
            if e.stderr: print("STDERR:\n", e.stderr)
        # ---------------------------------------------
        sys.exit(1)
    except FileNotFoundError:
        print(f"!!! ERROR: Command '{cmd[0]}' not found. Make sure it's installed and in your system's PATH.")
        sys.exit(1)

def create_and_activate_venv():
    """Creates a virtual environment and provides activation command."""
    if not PYTHON_VENV_DIR.exists():
        print(f"Creating virtual environment at {PYTHON_VENV_DIR}...")
        run_command([sys.executable, "-m", "venv", str(PYTHON_VENV_DIR)])
        print("Virtual environment created.")
    else:
        print(f"Virtual environment already exists at {PYTHON_VENV_DIR}.")

    # Provide activation instructions
    print("\n--------------------------------------------------------------")
    print("  Please activate the virtual environment in your terminal:")
    if sys.platform == "win32":
        print(f"  {PYTHON_VENV_DIR.name}\\Scripts\\activate.bat")
    else:
        print(f"  source {PYTHON_VENV_DIR.name}/bin/activate")
    print("  Then, run this script again.")
    print("--------------------------------------------------------------")
    sys.exit(0) # Exit and let user activate venv


def install_python_dependencies():
    """Installs dependencies from requirements.txt."""
    req_file = PROJECT_ROOT / "requirements.txt"
    if not req_file.exists():
        print(f"!!! ERROR: {req_file} not found. Please run 'pip freeze > requirements.txt' in your project directory first.")
        sys.exit(1)

    print("Installing/updating Python dependencies...")
    # Ensure pip is up-to-date in the venv
    run_command([sys.executable, "-m", "pip", "install", "--upgrade", "pip"])
    # Install from requirements.txt
    run_command([sys.executable, "-m", "pip", "install", "-r", str(req_file)])
    # Install pyinstaller (might not be in requirements.txt)
    run_command([sys.executable, "-m", "pip", "install", "pyinstaller"])
    print("Python dependencies installed.")


def uninstall_typing_package():
    """Attempts to uninstall the problematic 'typing' package."""
    print("Checking for and attempting to uninstall 'typing' package...")
    try:
        # Use check=False so the script doesn't exit if 'typing' isn't found (it's not an error then)
        run_command([sys.executable, "-m", "pip", "uninstall", "typing", "-y"], check=False, capture_output=True) # -y answers yes automatically
        # Verify it's gone by listing packages and checking output
        result = run_command([sys.executable, "-m", "pip", "list"], capture_output=True)
        if "typing" in result.stdout:
             print("!!! WARNING: 'typing' package still listed after uninstall attempt.")
             print("Please check your requirements.txt or other dependencies that might be reinstalling it.")
        else:
             print("'typing' package successfully uninstalled or was not present.")

    except Exception as e:
        print(f"!!! ERROR: An error occurred during 'typing' uninstall attempt: {e}")

def download_and_setup_ffmpeg():
    """Downloads FFmpeg and places it in the project root."""
    print("Checking FFmpeg setup...")

    ffmpeg_exe_path = PROJECT_ROOT / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    
    # Check if ffmpeg.exe already exists and seems recent
    if ffmpeg_exe_path.exists():
        print(f"FFmpeg executable already exists at {ffmpeg_exe_path}. Skipping download.")
        # You could add a check for file size or modify date if you want to force re-download
        return

    print(f"Downloading FFmpeg from {FFMPEG_DOWNLOAD_URL}...")
    try:
        response = requests.get(FFMPEG_DOWNLOAD_URL, stream=True)
        response.raise_for_status() # Raise an exception for bad status codes

        download_path = PROJECT_ROOT / FFMPEG_ARCHIVE_NAME
        with open(download_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        print(f"Downloaded {FFMPEG_ARCHIVE_NAME}.")

        print(f"Extracting {FFMPEG_ARCHIVE_NAME} to {FFMPEG_EXTRACT_TEMP_DIR}...")
        if FFMPEG_EXTRACT_TEMP_DIR.exists():
            shutil.rmtree(FFMPEG_EXTRACT_TEMP_DIR) # Clean up previous extraction
        
        # Use zipfile for .zip archives. For .7z, you'd need a 7-zip command-line tool.
        with zipfile.ZipFile(download_path, 'r') as zip_ref:
            zip_ref.extractall(FFMPEG_EXTRACT_TEMP_DIR)
        print("Extraction complete.")

        # Find the bin folder inside the extracted directory (usually named like ffmpeg-XXXX/bin)
        ffmpeg_bin_source_path = None
        for root, dirs, files in os.walk(FFMPEG_EXTRACT_TEMP_DIR):
            if "ffmpeg.exe" in files or "ffmpeg" in files and Path(root).name == "bin":
                ffmpeg_bin_source_path = Path(root)
                break
        
        if not ffmpeg_bin_source_path:
            print("!!! ERROR: Could not find 'bin' folder with FFmpeg executable inside the extracted archive.")
            sys.exit(1)

        print(f"Copying FFmpeg executables and DLLs from {ffmpeg_bin_source_path} to {PROJECT_ROOT}...")
        copied_files = []
        for item in ffmpeg_bin_source_path.iterdir():
            if item.is_file() and (item.suffix.lower() == ".exe" or item.suffix.lower() == ".dll" or item.name == "ffmpeg"):
                shutil.copy(item, PROJECT_ROOT)
                copied_files.append(item.name)
        print(f"Copied: {', '.join(copied_files)}")

        # Clean up temporary extraction directory and downloaded archive
        print("Cleaning up temporary FFmpeg files...")
        shutil.rmtree(FFMPEG_EXTRACT_TEMP_DIR)
        os.remove(download_path)
        print("FFmpeg setup complete.")

    except requests.exceptions.RequestException as e:
        print(f"!!! ERROR: Failed to download FFmpeg. Check internet connection or URL: {e}")
        sys.exit(1)
    except zipfile.BadZipFile:
        print("!!! ERROR: Downloaded FFmpeg file is corrupted or not a valid zip archive.")
        sys.exit(1)
    except Exception as e:
        print(f"!!! ERROR: An unexpected error occurred during FFmpeg setup: {e}")
        sys.exit(1)


def setup_consumet_api():
    """Clones or updates Consumet API and installs its Node.js dependencies."""
    print("Setting up Consumet API...")
    if not CONSUMET_API_DIR.exists():
        print(f"Cloning Consumet API to {CONSUMET_API_DIR}...")
        run_command(["git", "clone", "https://github.com/consumet/consumet.git", str(CONSUMET_API_DIR)])
    else:
        print(f"Consumet API directory already exists. Pulling latest changes...")
        run_command(["git", "pull"], cwd=str(CONSUMET_API_DIR))
    
    print("Installing Consumet API Node.js dependencies (npm install)...")
    # Need to run npm install in the Consumet API directory
    if sys.platform == "win32":
        # On Windows, npm commands might need shell=True or be run in a separate cmd process if issues arise
        run_command(["npm", "install"], cwd=str(CONSUMET_API_DIR), shell=True) 
    else:
        run_command(["npm", "install"], cwd=str(CONSUMET_API_DIR))
    print("Consumet API setup complete.")

def generate_and_modify_spec():
    """Generates and customizes the PyInstaller .spec file."""
    print("Generating initial PyInstaller .spec file...")
    run_command([sys.executable, "-m", "PyInstaller", "--name", EXE_NAME, "--noconfirm", "app.py"])
    
    spec_file_path = PROJECT_ROOT / f"{EXE_NAME}.spec"
    if not spec_file_path.exists():
        print(f"!!! ERROR: PyInstaller did not create {spec_file_path}. Check previous steps.")
        sys.exit(1)

    print(f"Modifying {spec_file_path}...")
    with open(spec_file_path, 'r') as f:
        spec_content = f.read()

    # Add datas entries for static, templates, ffmpeg.exe, and its DLLs
    # Dynamically find all DLLs in the project root
    ffmpeg_exe_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    all_dlls = [f for f in os.listdir(PROJECT_ROOT) if Path(f).suffix.lower() == ".dll" or f == ffmpeg_exe_name]
    
    # Create the datas list entries
    datas_entries = [
        "('static', 'static')",
        "('templates', 'templates')"
    ]
    for dll in all_dlls:
        datas_entries.append(f"('{dll}', '.')") # Copy DLLs to bundle root

    # Join with commas and newlines for insertion
    datas_str = ",\n                     ".join(datas_entries)

    # Insert datas. Use regex to find the 'datas=[]' or 'datas=[],' line and replace it.
    # This regex is a bit fragile, ensure your .spec matches the expected format from pyinstaller --name --noconfirm
    spec_content = spec_content.replace("datas=[],", f"datas=[\n                     {datas_str}\n                 ],")
    spec_content = spec_content.replace("datas=[],", f"datas=[\n                     {datas_str}\n                 ],") # Try twice in case it's datas=[] without comma

    # Add common hiddenimports that PyInstaller might miss for Flask/requests etc.
    hidden_imports = [
        'flask_sqlalchemy', 'flask_login', 'flask_wtf', 'wtforms',
        'werkzeug.routing', 'jinja2.ext', 'requests.adapters',
        'packaging.version', 'packaging.specifiers', 'packaging.requirements'
    ]
    hidden_imports_str = ", ".join(f"'{imp}'" for imp in hidden_imports)
    
    # Replace hiddenimports=[] with our list
    spec_content = spec_content.replace("hiddenimports=[],", f"hiddenimports=[{hidden_imports_str}],")
    spec_content = spec_content.replace("hiddenimports=[],", f"hiddenimports=[{hidden_imports_str}],") # Try twice

    # Set console=True for debugging output during initial testing of EXE
    spec_content = spec_content.replace("console=False,", "console=True,")
    spec_content = spec_content.replace("console=False)", "console=True)") # For the last line

    # Optional: Add an icon path
    # if (PROJECT_ROOT / "app_icon.ico").exists(): # Assuming you have an app_icon.ico
    #    spec_content = spec_content.replace("icon=None", "icon='app_icon.ico'")

    with open(spec_file_path, 'w') as f:
        f.write(spec_content)
    print(".spec file modified successfully.")

def build_executable():
    """Builds the executable using the .spec file."""
    print("Building executable...")
    spec_file_path = PROJECT_ROOT / f"{EXE_NAME}.spec"
    run_command([sys.executable, "-m", "PyInstaller", str(spec_file_path)])
    print(f"Executable built. Find it in {PROJECT_ROOT / 'dist' / EXE_NAME}")

def start_consumet_process_daemon():
    """Starts the Consumet API in a background process."""
    global CONSUMET_PROCESS
    if CONSUMET_PROCESS is not None:
        print("Consumet API already running or attempted to start.")
        return

    print("Attempting to start Consumet API in background...")
    
    # Define Node.js executable path (assumes Node is in PATH or bundled later)
    node_cmd = "node"
    if sys.platform == "win32":
        node_cmd = "node.exe"
    
    # Find the main script (e.g., index.js or app.js in Consumet root)
    # Most Consumet setups have a package.json "main" or "start" script
    # For a direct node run, it's often index.js
    consumet_main_script = CONSUMET_API_DIR / "index.js" # Assuming index.js is the main script
    if not consumet_main_script.exists():
        print(f"!!! WARNING: Could not find Consumet main script at {consumet_main_script}. Cannot start Consumet API.")
        return

    try:
        # Popen is used to run in background. stdout/stderr are redirected to avoid blocking.
        # creationflags=subprocess.DETACHED_PROCESS allows the child process to outlive the parent.
        # However, for cleanup with atexit, it's often better to not detach.
        # subprocess.CREATE_NO_WINDOW hides the console on Windows.
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW # Hides the console window
        
        # Ensure npm start uses the correct environment variables, especially if running in a shell.
        # Also need to run it from the Consumet API directory.
        
        # Alternative 1: Direct node execution (more controlled)
        CONSUMET_PROCESS = subprocess.Popen(
            [node_cmd, str(consumet_main_script)],
            cwd=str(CONSUMET_API_DIR),
            stdout=subprocess.DEVNULL, # Redirect stdout to devnull
            stderr=subprocess.DEVNULL, # Redirect stderr to devnull
            stdin=subprocess.DEVNULL,  # Redirect stdin to devnull
            creationflags=flags
        )
        
        # Alternative 2: Using npm start (relies on package.json script)
        # This one is more robust if Consumet's start script has logic beyond just "node index.js"
        # For npm commands, often best to use shell=True on Windows.
        # CONSUMET_PROCESS = subprocess.Popen(
        #     ["npm", "start"],
        #     cwd=str(CONSUMET_API_DIR),
        #     shell=True, # Often required for npm commands on Windows
        #     stdout=subprocess.DEVNULL,
        #     stderr=subprocess.DEVNULL,
        #     stdin=subprocess.DEVNULL,
        #     creationflags=subprocess.CREATE_NO_WINDOW # This might not work if shell=True is active
        # )

        print(f"Consumet API process started with PID: {CONSUMET_PROCESS.pid}")
        # Register a cleanup function to stop Consumet when this script exits
        atexit.register(stop_consumet_process_daemon)
        time.sleep(5) # Give Consumet a moment to start up
        print("Consumet API should be running on http://localhost:3000")
    except Exception as e:
        print(f"!!! ERROR: Failed to start Consumet API process: {e}")
        CONSUMET_PROCESS = None

def stop_consumet_process_daemon():
    """Stops the Consumet API background process."""
    global CONSUMET_PROCESS
    if CONSUMET_PROCESS:
        print(f"Terminating Consumet API process (PID: {CONSUMET_PROCESS.pid})...")
        CONSUMET_PROCESS.terminate()
        try:
            CONSUMET_PROCESS.wait(timeout=10) # Give it 10 seconds to terminate gracefully
            print("Consumet API process terminated gracefully.")
        except subprocess.TimeoutExpired:
            print("Consumet API process did not terminate gracefully, killing it.")
            CONSUMET_PROCESS.kill()
            CONSUMET_PROCESS.wait() # Wait for it to be truly dead
            print("Consumet API process killed.")
        CONSUMET_PROCESS = None

# --- Main Script Execution ---

if __name__ == "__main__":
    print("--- Starting BingeWatch Automation Script ---")

    # 1. Create and/or activate virtual environment
    # This step requires user interaction. The script exits after providing instructions.
    # The user must re-run the script *after* activating the venv.
    if not (sys.prefix != sys.base_prefix): # Check if outside a venv
        create_and_activate_venv() # This function calls sys.exit(0)

    # If we reach here, a virtual environment should be active.
    print(f"Active Python interpreter: {sys.executable}")
    print(f"Virtual environment active at: {sys.prefix}")

    # 2. Install Python dependencies

    uninstall_typing_package()

    install_python_dependencies()

    uninstall_typing_package()

    # 3. Download and setup FFmpeg
    download_and_setup_ffmpeg()

    # 4. Setup Consumet API (clone/update and npm install)
    setup_consumet_api()

    # 5. Generate and modify .spec file
    generate_and_modify_spec()

    # 6. Build the executable
    build_executable()

    print("\n--- Automation Script Finished ---")
    print("To run your BingeWatch application:")
    print(f"1. Navigate to: {PROJECT_ROOT / 'dist' / EXE_NAME}")
    print(f"2. Double-click '{EXE_NAME}.exe'")
    print("\nNOTE: Consumet API will need to be started manually in a separate terminal before running the EXE (or modify 'main' block to start it).")
    print(f"   To start Consumet API manually: cd {CONSUMET_API_DIR} && npm start")
    
    # You can uncomment the following line if you want this script to also
    # start the Consumet API and keep it running while YOU test the EXE.
    # However, for a user, you typically want the EXE itself to manage it.
    # start_consumet_process_daemon() 
    
    # The script will now exit. If you uncommented start_consumet_process_daemon,
    # the atexit.register call will ensure Consumet is stopped.