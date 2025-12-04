#!/bin/bash

# Stop script execution on any error
set -e

# --- Colors for output ---
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# --- Check if running as root ---
if [ "$EUID" -eq 0 ]; then
  echo -e "${RED}Error: This script should not be run as root or with sudo.${NC}"
  echo "Please run it as a regular user. It will ask for your password when it needs to install system packages."
  exit 1
fi

echo -e "${GREEN}--- Starting setup script for Logos AI Assistant ---${NC}"

# --- Check and install curl ---
echo -e "\n${YELLOW}Step 0: Checking for curl...${NC}"
if ! command -v curl &> /dev/null; then
    echo "curl not found. Installing..."
    sudo apt-get update
    sudo apt-get install -y curl
    echo -e "${GREEN}curl installed successfully.${NC}"
else
    echo -e "${GREEN}curl is already installed. Skipping installation.${NC}"
fi

# --- 1. Check and install uv ---
echo -e "\n${YELLOW}Step 1: Checking for uv package manager...${NC}"
if ! command -v uv &> /dev/null; then
    echo "uv not found. Installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    echo -e "${GREEN}uv installed successfully.${NC}"
else
    echo -e "${GREEN}uv is already installed. Skipping installation.${NC}"
fi

# --- 2. Configure PATH variable ---
# Add ~/.local/bin to PATH for the current session to make uv available immediately
export PATH="$HOME/.local/bin:$PATH"

echo -e "\n${YELLOW}Step 2: Checking PATH environment variable...${NC}"
if [[ ":$PATH:" != ".*:"$HOME"/.local/bin:"* ]]; then
    echo "Path '$HOME/.local/bin' not found in PATH."
    echo "Adding path to ~/.bashrc..."
    echo '' >> ~/.bashrc
    echo '# Add uv to PATH' >> ~/.bashrc
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
    echo -e "${GREEN}Path added successfully. Please restart your terminal after setup is complete for changes to take effect.${NC}"
else
    echo -e "${GREEN}Path '$HOME/.local/bin' is already configured in PATH.${NC}"
fi

# --- 3. Install Python dependencies ---
echo -e "\n${YELLOW}Step 3: Installing project dependencies...${NC}"
if [ -f "pyproject.toml" ]; then
    echo "pyproject.toml found. Creating virtual environment and installing packages..."
    # Use uv to sync dependencies, specifying the Python version
    uv sync --python 3.12.11
    echo -e "${GREEN}Virtual environment created and dependencies installed successfully.${NC}"
else
    echo -e "${RED}Error: The 'pyproject.toml' file was not found in the current directory.${NC}"
    echo "Please ensure you are running this script from the project's root folder, which contains 'pyproject.toml'."
    exit 1
fi

# --- 4. Install system libraries for Qt ---
echo -e "\n${YELLOW}Step 4: Installing system libraries for Qt (XCB)...${NC}"
echo "This will require your administrator (sudo) password."
sudo apt-get update
sudo apt-get install -y libxcb-cursor0 libxkbcommon-x11-0 libxcb-xinerama0 libxcb-xinput0 libxcb-icccm4 libxcb-image0 libxcb-util1
echo -e "${GREEN}System libraries installed successfully.${NC}"

# --- 5. Run Python Setup Wizard ---
echo -e "\n${YELLOW}Step 5: Launching initial setup wizard...${NC}"

# Get the absolute path of the directory where the script is located
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_EXE="$APP_DIR/.venv/bin/python"
MAIN_PY="$APP_DIR/main.py"
SETUP_CHOICES_FILE="$APP_DIR/setup_choices.json"

# Check if the Python executable exists
if [ ! -f "$PYTHON_EXE" ]; then
    echo -e "${RED}Error: Python executable not found at '$PYTHON_EXE'. Setup cannot proceed.${NC}"
    exit 1
fi

echo "Launching the GUI setup wizard. Please complete the steps in the new window."

"$PYTHON_EXE" "$MAIN_PY" --run-setup
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo -e "${GREEN}Setup wizard completed successfully.${NC}"
else
    echo -e "${YELLOW}Setup wizard was cancelled by the user. Skipping further integration.${NC}"
    exit 0
fi

# --- 6. Process Setup Choices and Finalize Installation ---
echo -e "\n${YELLOW}Step 6: Applying final setup choices...${NC}"
CREATE_GLOBAL_COMMAND=false
CREATE_DESKTOP_ENTRY=false
CREATE_DESKTOP_SHORTCUT=false

if [ -f "$SETUP_CHOICES_FILE" ]; then
    echo "Setup choices file found. Reading configuration..."
    if grep -q '"create_global_command": true' "$SETUP_CHOICES_FILE"; then
        CREATE_GLOBAL_COMMAND=true
    fi
    if grep -q '"create_desktop_entry": true' "$SETUP_CHOICES_FILE"; then
        CREATE_DESKTOP_ENTRY=true
    fi
    if grep -q '"create_desktop_shortcut": true' "$SETUP_CHOICES_FILE"; then
        CREATE_DESKTOP_SHORTCUT=true
    fi
else
    echo -e "${YELLOW}Warning: Setup choices file not found. Defaulting to no additional setup.${NC}"
fi

# Create global launcher if selected
if [ "$CREATE_GLOBAL_COMMAND" = true ]; then
    echo -e "\nCreating a global 'logos' command..."
    LAUNCHER_PATH="/usr/local/bin/logos"

    echo "This will require your administrator sudo password to create the launcher."
    # Use a here-document with sudo to create the launcher script.
    # This is more robust and readable than creating a long string variable.
    # The 'EOF' marker is unquoted, so variables from this script (like $APP_DIR) are expanded.
    # Variables for the final script (like $1) are escaped as \$1.
    sudo tee "$LAUNCHER_PATH" > /dev/null <<EOF
#!/bin/bash
APP_DIR="$APP_DIR"
# Check for help flag
if [[ "\$1" == "-h" || "\$1" == "--help" ]]; then
    cat << 'EOM'
Usage: logos [optional_path_to_folder]


Launches the Logos AI Assistant.
If an optional folder path is provided, the assistant will start in that directory.

Example: logos /home/YourUser/Documents/MyProject

Options:
  -h, --help    Show this help message and exit.
EOM
    exit 0
fi
# Launch the Python application, passing all arguments
exec "\$APP_DIR/.venv/bin/python" "\$APP_DIR/main.py" "\$@"
EOF

    sudo chmod +x "$LAUNCHER_PATH"
    echo -e "${GREEN}Global 'logos' command created successfully.${NC}"
else
    echo -e "${YELLOW}Skipping creation of global 'logos' command.${NC}"
fi

# Create Desktop Entry if selected
if [ "$CREATE_DESKTOP_ENTRY" = true ]; then
    echo -e "\nCreating Desktop Entry for application menu and context menu..."
    DESKTOP_ENTRY_PATH="$HOME/.local/share/applications/logos.desktop"
    ICON_PATH="$APP_DIR/assets/ico.png"

    # Ensure the target directory exists
    mkdir -p "$HOME/.local/share/applications"

    # The Exec command depends on whether the global command was created
    if [ "$CREATE_GLOBAL_COMMAND" = true ]; then
        EXEC_COMMAND="logos %F"
    else
        # If no global command, the desktop entry must call the script directly
        EXEC_COMMAND="'$APP_DIR/.venv/bin/python' '$APP_DIR/main.py' %F"
    fi

    # Create the .desktop file using a here-document.
    # This is safer than building a multi-line string.
    # MimeType=inode/directory; is crucial for adding the "Open with" context menu item for folders.
    cat > "$DESKTOP_ENTRY_PATH" << EOL
[Desktop Entry]
Version=1.0
Name=Logos AI Assistant
Comment=Your AI-powered desktop assistant
Exec=$EXEC_COMMAND
Icon=$ICON_PATH
Terminal=false
Type=Application
Categories=Utility;Development;
MimeType=inode/directory;
EOL

    echo -e "${GREEN}Desktop entry created at '$DESKTOP_ENTRY_PATH'${NC}"
    echo "Updating desktop database..."
    # Update the database to make sure the system recognizes the new entry
    update-desktop-database "$HOME/.local/share/applications"
    echo -e "${GREEN}Desktop database updated.${NC}"
else
    echo -e "${YELLOW}Skipping creation of Desktop Entry.${NC}"
fi

# Create Desktop Shortcut if selected
if [ "$CREATE_DESKTOP_SHORTCUT" = true ]; then
    echo -e "\nCreating Desktop Shortcut..."
    if [ -f "$DESKTOP_ENTRY_PATH" ]; then
        # Ensure the target directory exists
        mkdir -p "$HOME/Desktop"
        # Copy the .desktop file to the user's desktop
        cp "$DESKTOP_ENTRY_PATH" "$HOME/Desktop/"
        # Make the desktop shortcut executable
        chmod +x "$HOME/Desktop/logos.desktop"
        echo -e "${GREEN}Desktop shortcut created successfully.${NC}"
    else
        echo -e "${YELLOW}Warning: Cannot create desktop shortcut because the desktop entry was not created first. Skipping.${NC}"
    fi
else
    echo -e "${YELLOW}Skipping creation of Desktop Shortcut.${NC}"
fi

# Cleanup
if [ -f "$SETUP_CHOICES_FILE" ]; then
    rm "$SETUP_CHOICES_FILE"
    echo -e "\n${GREEN}Cleaned up temporary setup file.${NC}"
fi

# --- Final Completion Message ---
echo -e "\n${GREEN}--- Setup complete! ---${NC}"
echo "You can now run the application:"
if [ "$CREATE_DESKTOP_ENTRY" = true ]; then
    echo -e "1. From your system application menu."
fi
if [ "$CREATE_GLOBAL_COMMAND" = true ]; then
    echo -e "2. Using the global command: ${YELLOW}logos [optional_path_to_folder]${NC}"
fi
echo "3. Manually by activating the environment and running main.py:"
echo -e "   - Activate: ${YELLOW}source .venv/bin/activate${NC}"
echo -e "   - Run:      ${YELLOW}python main.py [optional_path_to_folder]${NC}"