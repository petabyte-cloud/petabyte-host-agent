"""
Build script for creating standalone .exe file
"""
import PyInstaller.__main__
import os
import shutil

def build_exe():
    """Build the standalone executable."""
    print("Building Petabyte Agent executable...")
    
    # PyInstaller arguments
    args = [
        'main.py',
        '--name=PetabyteAgent',
        '--onefile',
        '--windowed',  # No console window
        '--add-data=templates;templates',  # Include templates
        '--hidden-import=cli_ui',
        '--hidden-import=agent_cli',
        '--hidden-import=provision',
        '--hidden-import=crypto',
        '--hidden-import=flask',
        '--hidden-import=httpx',
        '--hidden-import=nbformat',
        '--hidden-import=nbclient',
        '--hidden-import=requests',
        '--collect-all=flask',
        '--collect-all=httpx',
        '--icon=NONE',  # Add icon file path if you have one
    ]
    
    try:
        PyInstaller.__main__.run(args)
        print("\n✅ Build complete! Executable is in the 'dist' folder.")
        print("   File: dist/PetabyteAgent.exe")
    except Exception as e:
        print(f"\n❌ Build failed: {e}")
        print("\nMake sure PyInstaller is installed:")
        print("   pip install pyinstaller")

if __name__ == "__main__":
    build_exe()

