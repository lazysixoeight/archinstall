from __future__ import annotations
import json
import logging
import os
import pathlib
import shlex
import time
from typing import Optional, List, Union, TYPE_CHECKING
# https://stackoverflow.com/a/39757388/929999
if TYPE_CHECKING:
	from .installer import Installer

from .disk import Partition, convert_device_to_uuid, MapperDev
from .general import SysCommand, SysCommandWorker
from .output import log
from .exceptions import DiskError

class luks2:
	def __init__(self,
		partition :Partition,
		mountpoint :str,
		password :Optional[Union[str, bytes]] = None,
		key_file :Optional[str] = None,
		auto_unmount :bool = False,
		*args :str,
		**kwargs :str):

		self.password = password
		self.key_file = key_file
		self.partition = partition
		self.mountpoint = mountpoint
		self.args = args
		self.kwargs = kwargs
		self.auto_unmount = auto_unmount
		self.filesystem = 'crypto_LUKS'
		self.mapdev = None

	def __enter__(self) -> Partition:
		if self.password is None and (self.key_file is None or pathlib.Path(self.key_file).exists() is False):
			raise AssertionError(f"luks2() requires either a `key_file` or a `password` parameter to operate.")

		if self.password is None:
			with pathlib.Path(self.key_file).resolve().open('rb') as pwfile:
				self.password = pwfile.read().rstrip(b'\r\n')

		if type(self.password) != bytes:
			self.password = bytes(self.password, 'UTF-8')

		return self.unlock(self.partition, self.mountpoint, self.password)

	def __exit__(self, *args :str, **kwargs :str) -> bool:
		# TODO: https://stackoverflow.com/questions/28157929/how-to-safely-handle-an-exception-inside-a-context-manager
		if self.auto_unmount:
			self.close()

		if len(args) >= 2 and args[1]:
			raise args[1]

		return True

	def encrypt(self, partition :Partition,
		password :Optional[Union[str, bytes]] = None,
		key_file :Optional[str] = None,
		key_size :int = 512,
		hash_type :str = 'sha512',
		iter_time :int = 10000) -> bool:

		log(f'Encrypting {partition} (This might take a while)', level=logging.INFO)

		if not password:
			password = self.password
		if not key_file:
			key_file = self.key_file

		if not any([password, key_file]):
			raise AssertionError(f"luks2().encrypt() requires either a `key_file` or a `password` parameter to operate.")

		if password is None and (key_file is None or pathlib.Path(key_file).exists() is False):
			raise AssertionError(f"luks2().encrypt() requires either a `key_file` or a `password` parameter to operate.")

		if password is None:
			with pathlib.Path(key_file).resolve().open('rb') as pwfile:
				password = pwfile.read().rstrip(b'\r\n')

		if type(password) != bytes:
			password = bytes(password, 'UTF-8')

		partition.partprobe()
		time.sleep(1)

		cryptsetup_args = shlex.join([
			'/usr/bin/cryptsetup',
			'--batch-mode',
			'--verbose',
			'--type', 'luks2',
			'--pbkdf', 'argon2id',
			'--hash', hash_type,
			'--key-size', str(key_size),
			'--iter-time', str(iter_time),
			# '--key-file', '/tmp/x.pw', # Reason: See issue #137
			'--use-urandom',
			'luksFormat', partition.path,
		])

		# print(f"Looking for phrase: 'Enter passphrase for {partition.path}'")
		cryptworker = SysCommandWorker(cryptsetup_args, peak_output=True)

		pw_given = False
		while cryptworker.is_alive():
			with open('debug_outer.txt', 'a') as silent_output:
				found = bytes(f'Enter passphrase for {partition.path}', 'UTF-8') in cryptworker
				silent_output.write(f"Found string in worker: {found} / {pw_given}")
				if found and pw_given is False:
					cryptworker.write(password)
					pw_given = True

		if cryptworker.exit_code == 256:
			log(f'{partition} is being used, trying to unmount and crypt-close the device and running one more attempt at encrypting the device: {cryptworker}', level=logging.INFO)
			# Partition was in use, unmount it and try again
			partition.unmount()

			# Get crypt-information about the device by doing a reverse lookup starting with the partition path
			# For instance: /dev/sda
			partition.partprobe()

			devinfo = json.loads(b''.join(SysCommand(f"lsblk --fs -J {partition.path}")).decode('UTF-8'))['blockdevices'][0]

			# For each child (sub-partition/sub-device)
			if len(children := devinfo.get('children', [])):
				for child in children:
					# Unmount the child location
					if child_mountpoint := child.get('mountpoint', None):
						log(f'Unmounting {child_mountpoint}', level=logging.DEBUG)
						SysCommand(f"umount -R {child_mountpoint}")

					# And close it if possible.
					log(f"Closing crypt device {child['name']}", level=logging.DEBUG)
					SysCommand(f"cryptsetup close {child['name']}")

			# Then try again to set up the crypt-device
			cryptworker = SysCommandWorker(cryptsetup_args)

			pw_given = False
			while cryptworker.is_alive():
				if not pw_given:
					cryptworker.write(password)
					pw_given = True
		elif cryptworker.exit_code > 0:
			raise DiskError(f"Could not encrypt {partition}: {cryptworker}")

		return True

	def unlock(self,
		partition :Partition,
		mountpoint :str,
		password :Optional[Union[str, bytes]] = None,
		key_file :Optional[str] = None) -> Partition:
		"""
		Mounts a luks2 compatible partition to a certain mountpoint.
		Keyfile must be specified as there's no way to interact with the pw-prompt atm.

		:param mountpoint: The name without absolute path, for instance "luksdev" will point to /dev/mapper/luksdev
		:type mountpoint: str
		"""

		if not password:
			password = self.password
		if not key_file:
			key_file = self.key_file

		if not any([password, key_file]):
			raise AssertionError(f"luks2().encrypt() requires either a `key_file` or a `password` parameter to operate.")

		if password is None and (key_file is None or pathlib.Path(key_file).exists() is False):
			raise AssertionError(f"luks2().encrypt() requires either a `key_file` or a `password` parameter to operate.")

		if password is None:
			with pathlib.Path(key_file).resolve().open('rb') as pwfile:
				password = pwfile.read().rstrip(b'\r\n')

		if type(password) != bytes:
			password = bytes(password, 'UTF-8')

		if '/' in mountpoint:
			os.path.basename(mountpoint)  # TODO: Raise exception instead?

		# print(f"Looking for phrase: 'Enter passphrase for {partition.path}'")
		cryptworker = SysCommandWorker(f'/usr/bin/cryptsetup open {partition.path} {mountpoint} --type luks2', peak_output=True)

		pw_given = False
		while cryptworker.is_alive():
			if bytes(f'Enter passphrase for {partition.path}', 'UTF-8') in cryptworker and pw_given is False:
				cryptworker.write(password)
				pw_given = True

		if not cryptworker.exit_code == 0:
			raise DiskError(f"Could not unlock {partition}: {cryptworker}")

		if os.path.islink(f'/dev/mapper/{mountpoint}'):
			self.mapdev = MapperDev(mountpoint)

			log(f"{partition} unlocked as {self.mapdev}")

			return self.mapdev

	def close(self, mountpoint :Optional[str] = None) -> bool:
		if not mountpoint and self.mapdev:
			mountpoint = self.mapdev.path

		if mountpoint:
			SysCommand(f'/usr/bin/cryptsetup close {mountpoint}')
		else:
			return False

		return os.path.islink(mountpoint) is False

	def format(self, path :str) -> None:
		if (handle := SysCommand(f"/usr/bin/cryptsetup -q -v luksErase {path}")).exit_code != 0:
			raise DiskError(f'Could not format {path} with {self.filesystem} because: {b"".join(handle)}')

	def add_key(self, path :pathlib.Path, password :str) -> bool:
		if not path.exists():
			raise OSError(2, f"Could not import {path} as a disk encryption key, file is missing.", str(path))

		log(f'Adding additional key-file {path} for {self.partition}', level=logging.INFO)
		worker = SysCommandWorker(f"/usr/bin/cryptsetup -q -v luksAddKey {self.partition.path} {path}",
							environment_vars={'LC_ALL':'C'})
		pw_injected = False
		while worker.is_alive():
			if b'Enter any existing passphrase' in worker and pw_injected is False:
				worker.write(bytes(password, 'UTF-8'))
				pw_injected = True

		if worker.exit_code != 0:
			raise DiskError(f'Could not add encryption key {path} to {self.partition} because: {worker}')

		return True

	def crypttab(self, installation :Installer, key_path :str, options :List[str] = ["luks", "key-slot=1"]) -> None:
		log(f'Adding a crypttab entry for key {key_path} in {installation}', level=logging.INFO)
		with open(f"{installation.target}/etc/crypttab", "a") as crypttab:
			crypttab.write(f"{self.mountpoint} UUID={convert_device_to_uuid(self.partition.path)} {key_path} {','.join(options)}\n")
