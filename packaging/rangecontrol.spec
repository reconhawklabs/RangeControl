# PyInstaller spec for RangeControl. Shared by the Linux and Windows builds.
#
# collect_all is used rather than bare hiddenimports for the provider SDKs:
# both anthropic and google-genai import submodules lazily and carry pydantic
# model data files. PyInstaller's static analysis misses them, and the failure
# mode is a binary that starts fine and dies on the first API call.
from PyInstaller.utils.hooks import collect_all

datas = [("../rangecontrol/RangeTemplate.md", "rangecontrol"),
         ("../rangecontrol/PREGENERATE.md", "rangecontrol")]
binaries = []
hiddenimports = []

# certifi is listed explicitly: the Linux binary is built in Debian bookworm,
# whose OpenSSL CA paths do not exist on Fedora/RHEL/Arch, so the bundled
# cacert.pem is the only CA store those platforms will find.
for package in ("anthropic", "google.genai", "discord", "pypdf", "openpyxl",
                "docx", "pptx", "PIL", "certifi"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

a = Analysis(
    ["../rangecontrol/__main__.py"],
    pathex=[".."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "_pytest", "matplotlib", "numpy"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="rangecontrol",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
