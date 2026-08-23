<p align="center">
  <img src="resources/logo/pysar.png" alt="Pysar logo" width="160">
</p>

<h1 align="center">Pysar</h1>

<p align="center">
  An editor for Nintendo Wii BRSAR sound archives.
</p>

Pysar is the first fully fledged editor for Nintendo Wii BRSAR archives! It gives streams, wave sounds, sequences, banks, players, groups, and wave archives a proper desktop interface, with playback and editing tools in the same place.

Version 1.1 is the current stable release. But please be aware that it may still contain annoying bugs. If you find a bug or have a question, please reach out to us on Discord:
`@ogu_99` or `@nin0_`.

This is an independent project and is not affiliated with or endorsed by Nintendo.

## What it can do

- Browse and search sounds, banks, groups, players, wave archives, and embedded files.
- Preview streams, wave sounds, individual samples, sequence variations, and bank notes.
- Import, replace, rename, reorganize, and remove archive resources.
- Work with Nintendo audio formats including BRSAR, BRSTM, BRSEQ, BRBNK, BRWAR, and BRWAV.
- Convert common editing formats such as WAV, MIDI, and SoundFont 2 where the underlying resource supports it.
- Export one sound at a time or dump a complete archive as original or converted assets.
- Use safe mode while exploring, then explicitly enable archive-changing operations when you are ready.

## Downloading Pysar

Ready-to-run builds are published on the [Releases page](https://github.com/Developers-Collective/Pysar/releases/latest) for 64-bit Windows and Linux.

### Windows

Download `Pysar-<version>-Windows-x64.zip`, extract it, and run `Pysar.exe` inside the `Pysar` folder. Keep the other extracted files beside the executable.

The Windows build is currently unsigned, so SmartScreen may warn the first time it is opened. Only choose **More info -> Run anyway** when the file came from this repository's Releases page.

### Linux

Pysar uses GTK 3 and WebKitGTK for its window. On Ubuntu or Debian, install the runtime libraries first:

```bash
sudo apt update
sudo apt install gir1.2-gtk-3.0 gir1.2-webkit2-4.1
```

Then unpack the release and start it:

```bash
tar -xzf Pysar-<version>-Linux-x64.tar.gz
cd Pysar
./Pysar
```

If your desktop or archive tool removed the executable bit, restore it with `chmod +x Pysar`.

There is no packaged macOS build at the moment. Pysar can still be run from source on macOS using the instructions below.

## Running from source

You will need Git and Python 3.11, 3.12, or 3.13. Python 3.12 is what the release builds use.

On Linux, install the GTK/WebKit runtime and the headers needed to install PyGObject:

```bash
sudo apt update
sudo apt install python3-dev pkg-config libcairo2-dev libgirepository1.0-dev \
  gir1.2-gtk-3.0 gir1.2-webkit2-4.1
```

Clone the repository and create a virtual environment:

```bash
git clone https://github.com/Developers-Collective/Pysar.git
cd Pysar
python -m venv .venv
```

Activate it on Windows (PowerShell):

```powershell
.venv\Scripts\Activate.ps1
```

Or on Linux and macOS:

```bash
source .venv/bin/activate
```

Install Pysar and start the application:

```bash
python -m pip install --upgrade pip
python -m pip install -e .
python pysar.py
```

After installation, the `pysar` command starts the same application. Set `PYSAR_DEBUG=1` before launching if you need pywebview's developer/debug mode.

## Credits

Pysar (c) 2026 made by
[Ogu99](https://github.com/Ogu-99) and
[Nin0](https://github.com/N-I-N-0)

Huge thanks to:
- [RedStoneMatt](https://github.com/RedStoneMatt) for providing additional technical and mental (xD) help
- [0D](https://github.com/redditchung) for extensive testing and bug reporting (he was taking it apart like Lego).

## License

Pysar is licensed under the [Mozilla Public License 2.0](LICENSE).

## Third-party software

Pysar includes React and ReactDOM 18.3.1 and Babel Standalone 7.29.0,
which are distributed under the MIT License. The sheet-music view bundles
OpenSheetMusicDisplay 1.9.9 under its BSD 3-Clause license. See
[third-party licenses](third_party_licenses/) for their complete terms.
