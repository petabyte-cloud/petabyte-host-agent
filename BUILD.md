# Building Petabyte Agent Executable

## Prerequisites

1. Python 3.8 or higher
2. All dependencies installed:
   ```bash
   pip install -r requirements.txt
   pip install pyinstaller
   ```

## Building the .exe

### Method 1: Using the build script

```bash
python build_exe.py
```

### Method 2: Manual PyInstaller command

```bash
pyinstaller --name=PetabyteAgent --onefile --windowed --add-data="templates;templates" --hidden-import=flask --hidden-import=httpx --hidden-import=nbformat --hidden-import=nbclient --hidden-import=requests --collect-all=flask --collect-all=httpx main.py
```

## Output

The executable will be created in the `dist` folder:
- `dist/PetabyteAgent.exe`

## Distribution

1. Copy `PetabyteAgent.exe` to the target machine
2. No additional files needed (all bundled in the .exe)
3. Run the executable - it will:
   - Start the agent
   - Launch the web UI on http://127.0.0.1:5000
   - Create a log file: `petabyte_agent.log`

## Troubleshooting

### Build fails with import errors
- Make sure all dependencies are installed
- Try adding more `--hidden-import` flags for missing modules

### Executable doesn't start
- Check `petabyte_agent.log` for errors
- Try running with `--console` instead of `--windowed` to see errors

### UI not accessible
- Check Windows Firewall settings
- Ensure port 5000 is not blocked
- Check the log file for connection errors

