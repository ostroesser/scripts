#!/usr/bin/env bash
# Make WSL trust the same certificate authorities as Windows (including corporate root CAs),
# for Python requests, pip, Hugging Face downloads, git and curl.
#
#   bash import-windows-certs.sh            # writes ~/.certs/bundle.pem and updates ~/.bashrc
#   bash import-windows-certs.sh --check https://mattermost.example.com
#
# No sudo needed. Re-run it if the company rotates its CAs.
set -euo pipefail

DEST="$HOME/.certs"
BUNDLE="$DEST/bundle.pem"
SYSTEM_BUNDLE=/etc/ssl/certs/ca-certificates.crt

check() {
  echo "Checking $1 …"
  SSL_CERT_FILE="$BUNDLE" python3 -c "import ssl,sys,urllib.request
ctx = ssl.create_default_context(cafile='$BUNDLE')
try:
    urllib.request.urlopen(sys.argv[1], context=ctx, timeout=15)
except urllib.error.HTTPError:
    pass          # any HTTP answer means TLS worked
print('TLS OK')" "$1"
}

if [[ "${1:-}" == "--check" ]]; then check "$2"; exit; fi

command -v powershell.exe >/dev/null || { echo "powershell.exe not found: run this inside WSL." >&2; exit 1; }
mkdir -p "$DEST"

echo "Exporting the Windows certificate stores …"
# Root and intermediate CAs, machine and user stores, as PEM on stdout.
# Kept on one line: multi-line PowerShell fed through stdin never runs.
PS_EXPORT='$s = "Cert:\LocalMachine\Root","Cert:\CurrentUser\Root","Cert:\LocalMachine\CA","Cert:\CurrentUser\CA"; Get-ChildItem $s -ErrorAction SilentlyContinue | Sort-Object Thumbprint -Unique | ForEach-Object { "-----BEGIN CERTIFICATE-----"; [Convert]::ToBase64String($_.RawData, "InsertLineBreaks"); "-----END CERTIFICATE-----" }'
powershell.exe -NoProfile -NonInteractive -Command "$PS_EXPORT" 2>"$DEST/powershell-errors.txt" \
  | tr -d '\r' > "$DEST/windows-ca.pem" || true

n=$(grep -c "BEGIN CERTIFICATE" "$DEST/windows-ca.pem" || true)

if [[ "$n" -eq 0 ]]; then
  # Fallback for locked-down PowerShell (ConstrainedLanguage blocks [Convert]):
  # export each certificate as a DER file with a standard cmdlet, convert to PEM here.
  echo "  direct export failed, trying Export-Certificate …"
  PS_FILES='$s = "Cert:\LocalMachine\Root","Cert:\CurrentUser\Root","Cert:\LocalMachine\CA","Cert:\CurrentUser\CA"; $d = Join-Path $env:TEMP "wsl-certs"; New-Item -ItemType Directory -Force -Path $d | Out-Null; Get-ChildItem $s -ErrorAction SilentlyContinue | Sort-Object Thumbprint -Unique | ForEach-Object { Export-Certificate -Cert $_ -FilePath (Join-Path $d ($_.Thumbprint + ".cer")) -Type CERT | Out-Null }; $d'
  WIN_DIR=$(powershell.exe -NoProfile -NonInteractive -Command "$PS_FILES" 2>>"$DEST/powershell-errors.txt" | tr -d '\r' | tail -1 || true)
  if [[ -n "$WIN_DIR" ]] && LIN_DIR=$(wslpath -u "$WIN_DIR" 2>/dev/null) && [[ -d "$LIN_DIR" ]]; then
    python3 -c "import ssl,sys,glob
for f in sorted(glob.glob(sys.argv[1] + '/*.cer')):
    sys.stdout.write(ssl.DER_cert_to_PEM_cert(open(f, 'rb').read()))" "$LIN_DIR" > "$DEST/windows-ca.pem"
    rm -rf "$LIN_DIR"
    n=$(grep -c "BEGIN CERTIFICATE" "$DEST/windows-ca.pem" || true)
  fi
fi

if [[ "$n" -eq 0 ]]; then
  echo "No certificates exported. PowerShell said:" >&2
  tr -d '\r' < "$DEST/powershell-errors.txt" | head -20 >&2
  echo >&2
  echo "PowerShell language mode (should be FullLanguage):" >&2
  powershell.exe -NoProfile -NonInteractive -Command '$ExecutionContext.SessionState.LanguageMode' 2>&1 | tr -d '\r' >&2 || true
  exit 1
fi
echo "  $n certificates from Windows"

# Public CAs from Linux + Windows/corporate CAs
{ [[ -f "$SYSTEM_BUNDLE" ]] && cat "$SYSTEM_BUNDLE"; cat "$DEST/windows-ca.pem"; } > "$BUNDLE"
echo "  bundle written to $BUNDLE"

MARK="# >>> windows certs (import-windows-certs.sh) >>>"
if ! grep -qF "$MARK" "$HOME/.bashrc" 2>/dev/null; then
  cat >> "$HOME/.bashrc" <<EOF

$MARK
export SSL_CERT_FILE="$BUNDLE"        # Python ssl, httpx (Hugging Face)
export REQUESTS_CA_BUNDLE="$BUNDLE"   # requests (mmscrape, scrapers)
export PIP_CERT="$BUNDLE"             # pip
export CURL_CA_BUNDLE="$BUNDLE"       # curl
export NODE_EXTRA_CA_CERTS="$BUNDLE"  # node / npm
# <<< windows certs <<<
EOF
  echo "  environment variables added to ~/.bashrc"
fi
git config --global http.sslCAInfo "$BUNDLE" 2>/dev/null && echo "  git configured" || true

echo
echo "Done. Open a new terminal (or run: source ~/.bashrc), then test with:"
echo "  bash $0 --check https://<your-mattermost-host>"
