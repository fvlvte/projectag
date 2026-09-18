#!/usr/bin/env python3
"""Encrypt standalone ZIP payloads before publication; never accept plaintext uploads."""
import argparse
import os
from pathlib import Path
import shutil
import struct
import tempfile
import zipfile


def require_encrypted(path):
    """Structural publication guard; encryption also verifies every authentication tag."""
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if not entries:
            raise ValueError('refusing an empty standalone archive')
        for entry in entries:
            extra = entry.extra
            aes = False
            while len(extra) >= 4:
                kind, size = struct.unpack('<HH', extra[:4])
                data, extra = extra[4:4 + size], extra[4 + size:]
                if kind == 0x9901:
                    aes = data == struct.pack('<H2sBH', 2, b'AE', 3, zipfile.ZIP_DEFLATED)
            if not (entry.flag_bits & 1 and entry.compress_type == 99 and aes):
                raise ValueError('refusing standalone ZIP without AES-256 AE-2 on every entry')


def password():
    value = os.environ.get('WINDOWS_STANDALONE_ZIP_PASSWORD', '').rstrip('\r\n')
    if len(value) < 32:
        raise ValueError('WINDOWS_STANDALONE_ZIP_PASSWORD must contain at least 32 characters; use a randomly generated secret')
    return value.encode('utf-8')


def encrypt(path):
    import pyzipper
    key = password()
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.encrypted-', suffix='.zip', dir=path.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(path) as source, pyzipper.AESZipFile(
            temporary, 'w', compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES,
        ) as target:
            target.setpassword(key)
            target.setencryption(pyzipper.WZ_AES, nbits=256)
            for entry in source.infolist():
                if entry.flag_bits & 1:
                    raise ValueError('input is already encrypted')
                with source.open(entry) as reader, target.open(entry.filename, 'w', force_zip64=True) as writer:
                    shutil.copyfileobj(reader, writer, 1024 * 1024)
        require_encrypted(temporary)
        with pyzipper.AESZipFile(temporary) as archive:
            archive.setpassword(key)
            for entry in archive.infolist():
                with archive.open(entry) as reader:
                    while reader.read(1024 * 1024):
                        pass
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['encrypt', 'verify', 'check-password'])
    parser.add_argument('path', nargs='?')
    args = parser.parse_args()
    try:
        if args.action == 'check-password':
            password()
        elif not args.path:
            raise ValueError('ZIP path required')
        elif args.action == 'encrypt':
            encrypt(args.path)
        else:
            require_encrypted(args.path)
    except (ValueError, OSError, RuntimeError, zipfile.BadZipFile) as error:
        parser.exit(1, f'error: {error}\n')


if __name__ == '__main__':
    main()
