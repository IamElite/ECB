import asyncio
import math
import os
import random
import shutil
import time
from hashlib import md5
from mimetypes import guess_extension
from pathlib import Path
from re import sub

from aiofiles import open as aiopen
from aiofiles.os import makedirs, remove
from pyrogram import raw, utils
from pyrogram.errors import AuthBytesInvalid, FloodWait
from pyrogram.file_id import PHOTO_TYPES, FileId, FileType, ThumbnailSource
from pyrogram.session import Auth, Session
from pyrogram.session.internals import MsgId
from asyncio import (
    CancelledError,
    create_task,
    gather,
    sleep,
    wait_for,
    TimeoutError as AsyncTimeoutError,
    Event,
    Lock,
    Queue,
    Semaphore,
)

HYPER_UL_MIN_SIZE = 50 * 1024 * 1024


class HyperTGDownload:
    """
    Parallel Telegram downloader — file ko multiple parts me download karta hai
    alag-alag media sessions se, fir merge karta hai.
    WZML-XDZ se adapted for single-client bot.
    """

    _load_lock = Lock()

    def __init__(self, client, num_parts=8):
        self.client = client
        self.num_parts = min(num_parts or 8, 8)
        self._per_task_limit = 8
        self.cache_file_ref = {}
        self.cache_last_access = {}
        self.cache_max_size = 100
        self._processed_bytes = 0
        self.file_size = 0
        self.chunk_size = 1024 * 1024  # 1MB per chunk
        self.file_name = ""
        self._cancel_event = Event()
        self._session_lock = Lock()
        self.session_pool = {}
        self.message = None
        self.dump_chat = None
        self.download_dir = "downloads/"
        self.directory = None
        create_task(self._clean_cache())

    @staticmethod
    async def get_media_type(message):
        media_types = ("audio", "document", "photo", "sticker", "animation", "video", "voice", "video_note", "new_chat_photo")
        for attr in media_types:
            if media := getattr(message, attr, None):
                return media
        raise ValueError("This message doesn't contain any downloadable media")

    def _update_cache(self, file_ref):
        idx = 0
        self.cache_file_ref[idx] = file_ref
        self.cache_last_access[idx] = time.time()
        if len(self.cache_file_ref) > self.cache_max_size:
            oldest = sorted(self.cache_last_access.items(), key=lambda x: x[1])[0][0]
            del self.cache_file_ref[oldest]
            del self.cache_last_access[oldest]

    async def get_specific_file_ref(self, mid, client, max_retries=3):
        retries = 0
        last_error = None
        while retries < max_retries:
            try:
                media = await client.get_messages(self.dump_chat, mid)
                return FileId.decode(getattr(await self.get_media_type(media), "file_id", ""))
            except Exception as e:
                last_error = e
                retries += 1
                await sleep(1 * retries)
        raise ValueError(f"Failed to get message {mid}. Error: {last_error}")

    async def get_file_id(self, client) -> FileId:
        if 0 not in self.cache_file_ref:
            file_ref = await self.get_specific_file_ref(self.message.id, client)
            self._update_cache(file_ref)
        else:
            self.cache_last_access[0] = time.time()
        return self.cache_file_ref[0]

    async def _clean_cache(self):
        while True:
            await sleep(15 * 60)
            current_time = time.time()
            expired_keys = [k for k, v in self.cache_last_access.items() if current_time - v > 45 * 60]
            for key in expired_keys:
                if key in self.cache_file_ref:
                    del self.cache_file_ref[key]
                if key in self.cache_last_access:
                    del self.cache_last_access[key]

    async def generate_media_session(self, client, file_id, max_retries=3):
        session_key = file_id.dc_id
        async with self._session_lock:
            if session_key in self.session_pool:
                return self.session_pool[session_key]
            retries = 0
            while retries < max_retries:
                try:
                    if file_id.dc_id != await client.storage.dc_id():
                        media_session = Session(client, file_id.dc_id, await Auth(client, file_id.dc_id, await client.storage.test_mode()).create(), await client.storage.test_mode(), is_media=True)
                        await media_session.start()
                        for _ in range(6):
                            exported_auth = await client.invoke(raw.functions.auth.ExportAuthorization(dc_id=file_id.dc_id))
                            try:
                                await media_session.invoke(raw.functions.auth.ImportAuthorization(id=exported_auth.id, bytes=exported_auth.bytes))
                                break
                            except AuthBytesInvalid:
                                await sleep(1)
                        else:
                            await media_session.stop()
                            raise AuthBytesInvalid
                    else:
                        media_session = Session(client, file_id.dc_id, await client.storage.auth_key(), await client.storage.test_mode(), is_media=True)
                        await media_session.start()
                    self.session_pool[session_key] = media_session
                    return media_session
                except Exception:
                    retries += 1
                    await sleep(1)
            raise ValueError(f"Failed to create media session after {max_retries} attempts")

    @staticmethod
    async def get_location(file_id: FileId):
        file_type = file_id.file_type
        if file_type == FileType.CHAT_PHOTO:
            if file_id.chat_id > 0:
                peer = raw.types.InputPeerUser(user_id=file_id.chat_id, access_hash=file_id.chat_access_hash)
            else:
                peer = raw.types.InputPeerChat(chat_id=-file_id.chat_id) if file_id.chat_access_hash == 0 else raw.types.InputPeerChannel(channel_id=utils.get_channel_id(file_id.chat_id), access_hash=file_id.chat_access_hash)
            return raw.types.InputPeerPhotoFileLocation(peer=peer, volume_id=file_id.volume_id, local_id=file_id.local_id, big=file_id.thumbnail_source == ThumbnailSource.CHAT_PHOTO_BIG)
        elif file_type == FileType.PHOTO:
            return raw.types.InputPhotoFileLocation(id=file_id.media_id, access_hash=file_id.access_hash, file_reference=file_id.file_reference, thumb_size=file_id.thumbnail_size)
        else:
            return raw.types.InputDocumentFileLocation(id=file_id.media_id, access_hash=file_id.access_hash, file_reference=file_id.file_reference, thumb_size=file_id.thumbnail_size)

    async def get_file(self, offset_bytes, first_part_cut, last_part_cut, part_count, max_retries=5):
        client = self.client
        try:
            if self._cancel_event.is_set():
                raise CancelledError("Download cancelled")
            file_id = await self.get_file_id(client)
            media_session, location = await gather(
                self.generate_media_session(client, file_id),
                self.get_location(file_id),
            )
            current_part = 1
            current_offset = offset_bytes
            while current_part <= part_count:
                if self._cancel_event.is_set():
                    raise CancelledError("Download cancelled")
                try:
                    r = await wait_for(
                        media_session.invoke(raw.functions.upload.GetFile(location=location, offset=current_offset, limit=self.chunk_size)),
                        timeout=60,
                    )
                    if isinstance(r, raw.types.upload.File):
                        chunk = r.bytes
                        if not chunk:
                            break
                        if part_count == 1:
                            yield chunk[first_part_cut:last_part_cut]
                        elif current_part == 1:
                            yield chunk[first_part_cut:]
                        elif current_part == part_count:
                            yield chunk[:last_part_cut]
                        else:
                            yield chunk
                        current_part += 1
                        current_offset += self.chunk_size
                        self._processed_bytes += len(chunk)
                    else:
                        raise ValueError(f"Unexpected response: {r}")
                except (FloodWait, AsyncTimeoutError, ConnectionError, RuntimeError) as e:
                    if isinstance(e, FloodWait):
                        await sleep(e.value + 1)
                        continue
                    if isinstance(e, (AsyncTimeoutError, ConnectionError, RuntimeError)):
                        session_key = file_id.dc_id
                        async with self._session_lock:
                            self.session_pool.pop(session_key, None)
                        self.cache_file_ref.pop(0, None)
                        try:
                            await media_session.stop()
                        except:
                            pass
                    raise
            if current_part <= part_count:
                raise ValueError(f"Incomplete download: got {current_part-1} of {part_count} parts")
        except (AsyncTimeoutError, ConnectionError, AttributeError, RuntimeError) as e:
            if "file_id" in locals():
                session_key = file_id.dc_id
                async with self._session_lock:
                    self.session_pool.pop(session_key, None)
                self.cache_file_ref.pop(0, None)
            raise

    async def progress_callback(self, progress, progress_args):
        if not progress:
            return
        while not self._cancel_event.is_set():
            try:
                if callable(progress):
                    await progress(self._processed_bytes, self.file_size, *progress_args)
                await sleep(1)
            except (CancelledError, StopTransmission):
                break
            except Exception:
                await sleep(1)

    async def single_part(self, start, end, part_index, max_retries=3):
        until_bytes, from_bytes = min(end, self.file_size - 1), start
        offset = from_bytes - (from_bytes % self.chunk_size)
        first_part_cut = from_bytes - offset
        last_part_cut = (until_bytes % self.chunk_size) + 1
        part_count = math.ceil(until_bytes / self.chunk_size) - math.floor(offset / self.chunk_size)
        part_file_path = os.path.join(self.directory, f"{self.file_name}.temp.{part_index:02d}")
        part_bytes = 0
        for attempt in range(max_retries):
            try:
                self._processed_bytes -= part_bytes
                part_bytes = 0
                async with aiopen(part_file_path, "wb") as f:
                    async for chunk in self.get_file(offset, first_part_cut, last_part_cut, part_count):
                        if self._cancel_event.is_set():
                            raise CancelledError("Download cancelled")
                        await f.write(chunk)
                return part_index, part_file_path
            except (AsyncTimeoutError, ConnectionError, RuntimeError, AttributeError):
                if attempt == max_retries - 1:
                    raise
                await sleep((attempt + 1) * 2)

    async def handle_download(self, progress, progress_args):
        self._cancel_event.clear()
        await makedirs(self.directory, exist_ok=True)
        temp_file_path = os.path.abspath(sub("\\\\", "/", os.path.join(self.directory, self.file_name))) + ".temp"
        num_parts = min(self.num_parts, max(1, self.file_size // (10 * 1024 * 1024)))
        if self.file_size < 10 * 1024 * 1024:
            num_parts = 1
        part_size = self.file_size // num_parts if num_parts > 0 else self.file_size
        ranges = [(i * part_size, min((i + 1) * part_size - 1, self.file_size - 1)) for i in range(num_parts)]
        tasks = []
        prog_task = None
        try:
            for i, (start, end) in enumerate(ranges):
                tasks.append(create_task(self.single_part(start, end, i)))
            if progress:
                prog_task = create_task(self.progress_callback(progress, progress_args))
            results = await gather(*tasks)
            async with aiopen(temp_file_path, "wb") as temp_file:
                for _, part_file_path in sorted(results, key=lambda x: x[0]):
                    async with aiopen(part_file_path, "rb") as part_file:
                        while True:
                            chunk = await part_file.read(8 * 1024 * 1024)
                            if not chunk:
                                break
                            await temp_file.write(chunk)
                    await remove(part_file_path)
            if prog_task and not prog_task.done():
                prog_task.cancel()
            file_path = os.path.splitext(temp_file_path)[0]
            await asyncio.to_thread(shutil.move, temp_file_path, file_path)
            return file_path
        except FloodWait:
            raise
        except (CancelledError, StopTransmission):
            return None
        except Exception as e:
            print(f"HyperDL Error: {e}")
            return None
        finally:
            self._cancel_event.set()
            if prog_task and not prog_task.done():
                prog_task.cancel()
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await gather(*tasks, return_exceptions=True)
            if prog_task:
                await gather(prog_task, return_exceptions=True)
            async with self._session_lock:
                sessions = list(self.session_pool.values())
                self.session_pool.clear()
            for session in sessions:
                try:
                    await session.stop()
                except Exception:
                    pass
            self.session_pool.clear()
            for i in range(len(ranges)):
                part_path = os.path.join(self.directory, f"{self.file_name}.temp.{i:02d}")
                try:
                    if os.path.exists(part_path):
                        await remove(part_path)
                except Exception:
                    pass

    @staticmethod
    async def get_extension(file_type, mime_type):
        if file_type in PHOTO_TYPES:
            return ".jpg"
        if mime_type:
            extension = guess_extension(mime_type)
            if extension:
                return extension
        if file_type == FileType.VOICE:
            return ".ogg"
        elif file_type in (FileType.VIDEO, FileType.ANIMATION, FileType.VIDEO_NOTE):
            return ".mp4"
        elif file_type == FileType.DOCUMENT:
            return ".bin"
        elif file_type == FileType.STICKER:
            return ".webp"
        elif file_type == FileType.AUDIO:
            return ".mp3"
        else:
            return ".bin"

    async def download_media(self, message, file_name="downloads/", progress=None, progress_args=(), dump_chat=None):
        try:
            if dump_chat:
                copied = await self.client.copy_message(chat_id=dump_chat, from_chat_id=message.chat.id, message_id=message.id, disable_notification=True)
                self.message = await self.client.get_messages(chat_id=dump_chat, message_ids=copied.id)
            self.dump_chat = dump_chat or message.chat.id
            self.message = self.message or message
            media = await self.get_media_type(self.message)
            file_id_str = media if isinstance(media, str) else media.file_id
            file_id_obj = FileId.decode(file_id_str)
            file_type = file_id_obj.file_type
            media_file_name = getattr(media, "file_name", "")
            self.file_size = getattr(media, "file_size", 0)
            mime_type = getattr(media, "mime_type", "image/jpeg")
            date = getattr(media, "date", None)
            self.directory, self.file_name = os.path.split(file_name)
            self.file_name = self.file_name or media_file_name or ""
            if not os.path.isabs(self.file_name):
                self.directory = Path(os.getcwd()).parent / (self.directory or self.download_dir)
            if not self.file_name:
                extension = await self.get_extension(file_type, mime_type)
                self.file_name = f"{FileType(file_id_obj.file_type).name.lower()}_{(date or time.time())}_{MsgId()}{extension}"
            return await self.handle_download(progress, progress_args)
        except Exception as e:
            print(f"Download media error: {e}")
            raise


class HyperTGUpload:
    """
    Parallel Telegram uploader — file ko multiple parts me upload karta hai
    alag-alag sessions se same DC par. 50MB+ files ke liye parallel hota hai.
    WZML-XDZ se adapted for single-client bot.
    """

    _global_semaphore = Semaphore(16)

    def __init__(self, num_workers=6):
        self.num_workers = num_workers
        self._per_task_limit = 8
        if not num_workers:
            self.num_workers = min(self.num_workers, self._per_task_limit)
        self._processed_bytes = 0
        self.file_size = 0
        self._cancel_event = Event()
        self._session_lock = Lock()
        self._sessions = []
        self._start_time = time.time()

    @property
    def processed_bytes(self):
        return self._processed_bytes

    @property
    def speed(self):
        elapsed = time.time() - self._start_time
        return self._processed_bytes / elapsed if elapsed > 0 else 0

    def cancel(self):
        self._cancel_event.set()

    async def _start_session(self, client, dc_id, auth_key, test_mode, i):
        try:
            session = Session(client, dc_id, auth_key, test_mode, is_media=True)
            await session.start()
            return session
        except Exception as e:
            print(f"HyperUL: Failed to start session {i}: {e}")
            return None

    async def _upload_worker(self, worker_id, client, queue, file_path, file_id, is_big, total_parts, chunk_size):
        async with self._global_semaphore:
            session = None
            try:
                dc_id = await client.storage.dc_id()
                auth_key = await client.storage.auth_key()
                test_mode = await client.storage.test_mode()
                session = await self._start_session(client, dc_id, auth_key, test_mode, worker_id)
                if not session:
                    return
                async with self._session_lock:
                    self._sessions.append(session)
            except Exception as e:
                print(f"HyperUL: Worker {worker_id} session init failed: {e}")
                return
            try:
                async with aiopen(file_path, "rb") as f:
                    while True:
                        part_no = -1
                        try:
                            part_no = await queue.get()
                            if part_no is None:
                                queue.task_done()
                                break
                            if self._cancel_event.is_set():
                                queue.task_done()
                                break
                            await f.seek(part_no * chunk_size)
                            data = await f.read(chunk_size)
                            if not data:
                                queue.task_done()
                                break
                            if is_big:
                                rpc = raw.functions.upload.SaveBigFilePart(file_id=file_id, file_part=part_no, file_total_parts=total_parts, bytes=data)
                            else:
                                rpc = raw.functions.upload.SaveFilePart(file_id=file_id, file_part=part_no, bytes=data)
                            for attempt in range(3):
                                try:
                                    success = await session.invoke(rpc)
                                    if success:
                                        break
                                except Exception as invoke_err:
                                    if attempt == 2:
                                        raise invoke_err
                                    await sleep(1 * (attempt + 1))
                            self._processed_bytes += len(data)
                            queue.task_done()
                        except CancelledError:
                            break
                        except Exception as e:
                            if self._cancel_event.is_set():
                                queue.task_done()
                                break
                            if part_no != -1:
                                is_transport_err = any(x in str(e).lower() for x in ["handler is closed", "broken pipe", "connection reset", "socket closed", "peer reset"]) or isinstance(e, (ConnectionError, RuntimeError))
                                print(f"HyperUL: Worker {worker_id} error on part {part_no}: {e}")
                                if is_transport_err:
                                    await queue.put(part_no)
                                    queue.task_done()
                                    try:
                                        await session.stop()
                                        session = await self._start_session(client, dc_id, auth_key, test_mode, worker_id)
                                        if not session:
                                            break
                                        continue
                                    except Exception as rec_err:
                                        print(f"HyperUL: Worker {worker_id} recovery failed: {rec_err}")
                                        break
                                else:
                                    queue.task_done()
                                    self._cancel_event.set()
                                    break
            finally:
                if session:
                    try:
                        await session.stop()
                        async with self._session_lock:
                            if session in self._sessions:
                                self._sessions.remove(session)
                    except:
                        pass

    async def save_file(self, client, path, progress=None, progress_args=()):
        self.file_size = os.path.getsize(path)
        file_name = os.path.basename(path)
        self._processed_bytes = 0
        self._cancel_event.clear()
        is_big = self.file_size > 10 * 1024 * 1024
        chunk_size = 512 * 1024
        total_parts = math.ceil(self.file_size / chunk_size)
        if total_parts > 8000:
            chunk_size = 1024 * 1024
            total_parts = math.ceil(self.file_size / chunk_size)
        file_id = random.randint(0, (2**63) - 1)
        if not self.num_workers:
            if self.file_size > 500 * 1024 * 1024:
                self.num_workers = 8
            if self.file_size > 2 * 1024 * 1024 * 1024:
                self.num_workers = 12
        num_workers = min(self.num_workers, total_parts, 12 if self.num_workers else self._per_task_limit)
        if self.file_size < HYPER_UL_MIN_SIZE:
            num_workers = 1
        print(f"HyperUL: file={file_name} size={(self.file_size/1024/1024):.1f}MB parts={total_parts} workers={num_workers}")
        queue = Queue(maxsize=num_workers * 2)
        self._start_time = time.time()
        workers = []
        for i in range(num_workers):
            worker = create_task(self._upload_worker(i, client, queue, path, file_id, is_big, total_parts, chunk_size))
            workers.append(worker)
        prog_task = None
        if progress:
            async def _progress_loop():
                while not self._cancel_event.is_set():
                    try:
                        await progress(self._processed_bytes, self.file_size, *progress_args)
                    except Exception:
                        pass
                    await sleep(1)
            prog_task = create_task(_progress_loop())
        try:
            for part_no in range(total_parts):
                if self._cancel_event.is_set():
                    break
                await queue.put(part_no)
            for _ in workers:
                await queue.put(None)
            await gather(*workers)
        except Exception as e:
            print(f"HyperUL: Upload queue failed: {e}")
            self._cancel_event.set()
            return None
        finally:
            if prog_task and not prog_task.done():
                prog_task.cancel()
            for session in self._sessions:
                try:
                    await session.stop()
                except Exception:
                    pass
            self._sessions.clear()
        if self._cancel_event.is_set():
            return None
        elapsed = time.time() - self._start_time
        speed_mbs = (self.file_size / (1024 * 1024)) / elapsed if elapsed > 0 else 0
        print(f"HyperUL: Upload complete | speed={speed_mbs:.1f}MB/s time={elapsed:.1f}s")
        if is_big:
            return raw.types.InputFileBig(id=file_id, parts=total_parts, name=file_name)
        else:
            md5_hash = md5()
            async with aiopen(path, "rb") as f:
                while True:
                    chunk = await f.read(1024 * 1024)
                    if not chunk:
                        break
                    md5_hash.update(chunk)
            return raw.types.InputFile(id=file_id, parts=total_parts, name=file_name, md5_checksum=md5_hash.hexdigest())
