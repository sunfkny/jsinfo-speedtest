import asyncio
import base64
import binascii
import json
import math
import re
import ssl
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from decimal import Decimal

import httpx2
import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TransferSpeedColumn,
)

app = typer.Typer()
console = Console()


def parse_quantity(
    value: str, units: dict[str, int], default_unit: str, option: str
) -> Decimal:
    match = re.fullmatch(r"\s*(\d+(?:\.\d*)?|\.\d+)\s*([a-zA-Z]*)\s*", value)
    if match is None:
        raise typer.BadParameter("请输入正数和支持的单位", param_hint=option)
    quantity, unit = match.groups()
    unit = match[2].lower() or default_unit
    if unit not in units:
        raise typer.BadParameter("不支持该单位", param_hint=option)
    quantity = Decimal(match[1]) * units[unit]
    if quantity <= 0:
        raise typer.BadParameter("必须大于 0", param_hint=option)
    return quantity


def parse_size(value: str | None, option: str) -> int | None:
    if value is None:
        return None
    units = {
        alias: 1024**power
        for power, aliases in (
            (2, ("m", "mb", "mib")),
            (3, ("g", "gb", "gib")),
            (4, ("t", "tb", "tib")),
        )
        for alias in aliases
    }
    size = int(parse_quantity(value, units, "mb", option))
    if size < 1:
        raise typer.BadParameter("大小不能小于 1 字节", param_hint=option)
    return size


def parse_duration(value: str | None, option: str) -> float | None:
    if value is None:
        return None
    duration = float(
        parse_quantity(value, {"s": 1, "m": 60, "h": 3600, "d": 86400}, "s", option)
    )
    if not math.isfinite(duration) or duration <= 0:
        raise typer.BadParameter("时间超出支持范围", param_hint=option)
    return duration


async def fetch_userinfo(c: httpx2.AsyncClient) -> dict:
    r = await c.get("http://speedauto.jsinfo.net/speedinfo/userinfo/1")
    r.raise_for_status()
    assert r.text
    text = r.text.strip()
    try:
        return json.loads(base64.b64decode(text))
    except binascii.Error:
        return json.loads(text)


async def detect_ip_protocol(c: httpx2.AsyncClient):
    r = await c.get("http://speedauto.jsinfo.net/speedinfo/checkip")
    r.raise_for_status()
    assert r.text
    data = r.text.strip().lower()

    if data == "ipv6":
        return data
    if data == "ipv4":
        return data
    raise RuntimeError(f"Unknown ipCheckAuto result: {data}")


def build_download_url(base: str, tid: int, download_size_mb: int) -> str:
    parsed = urllib.parse.urlparse(base)
    root = f"{parsed.scheme}://{parsed.netloc}"
    return f"{root}/backend/garbage.php?cors=true&r={time.time()}&ckSize={download_size_mb}&tid={tid}"


def build_upload_url(base: str) -> str:
    parsed = urllib.parse.urlparse(base)
    root = f"{parsed.scheme}://{parsed.netloc}"
    return f"{root}/backend/empty.php?cors=true&r={time.time()}"


async def run_workers(
    concurrency: int,
    worker: Callable[[int], Awaitable[None]],
    duration: float | None,
    transferred: list[int],
    progress: Progress | None,
    task_id: TaskID | None,
) -> float:
    async def update_progress() -> None:
        while True:
            if progress is not None and task_id is not None:
                progress.update(task_id, completed=transferred[0])
            await asyncio.sleep(0.1)

    start = time.perf_counter()
    deadline = asyncio.timeout(duration)
    updater = (
        asyncio.create_task(update_progress())
        if progress is not None and task_id is not None
        else None
    )
    tasks = []
    try:
        try:
            async with deadline:
                tasks = [asyncio.ensure_future(worker(i)) for i in range(concurrency)]
                await asyncio.gather(*tasks)
        except TimeoutError:
            if not deadline.expired():
                raise
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if updater is not None:
            updater.cancel()
            await asyncio.gather(updater, return_exceptions=True)
        if progress is not None and task_id is not None:
            progress.update(task_id, completed=transferred[0])
    return time.perf_counter() - start


async def run_download(
    urls: list[str],
    concurrency: int,
    progress: Progress | None = None,
    task_id: TaskID | None = None,
    bytes_ref: list[int] | None = None,
    size_bytes: int | None = None,
    duration: float | None = None,
) -> tuple[int, float]:
    transferred = bytes_ref if bytes_ref is not None else [0]

    async with httpx2.AsyncClient(
        verify=False,
        trust_env=False,
        follow_redirects=True,
        timeout=None,
        limits=httpx2.Limits(max_connections=concurrency),
    ) as c:

        async def worker(index: int) -> None:
            url = urls[index % len(urls)]
            remaining = worker_size(size_bytes, concurrency, index)
            while remaining is None or remaining > 0:
                received = 0
                async with c.stream("GET", url) as r:
                    r.raise_for_status()
                    async for chunk in r.aiter_bytes():
                        n = (
                            len(chunk)
                            if remaining is None
                            else min(len(chunk), remaining)
                        )
                        received += n
                        transferred[0] += n
                        if remaining is not None:
                            remaining -= n
                            if remaining == 0:
                                return
                if received == 0:
                    raise RuntimeError("下载服务器返回空数据")

        elapsed = await run_workers(
            concurrency, worker, duration, transferred, progress, task_id
        )
    return transferred[0], elapsed


def worker_size(size_bytes: int | None, concurrency: int, index: int) -> int | None:
    if size_bytes is None:
        return None
    quotient, remainder = divmod(size_bytes, concurrency)
    return quotient + (index < remainder)


class PutChunkedWriter:
    def __init__(self, url: str) -> None:
        parsed_url = urllib.parse.urlparse(url)
        self.host = parsed_url.hostname
        self.authority = parsed_url.netloc
        self.path = parsed_url.path or "/"
        if parsed_url.query:
            self.path += f"?{parsed_url.query}"
        self.tls = parsed_url.scheme == "https"
        self.port = (
            parsed_url.port
            or {
                "https": 443,
                "http": 80,
            }[parsed_url.scheme]
        )

    async def __aenter__(self):
        context = None
        if self.tls:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        self.reader, self.writer = await asyncio.open_connection(
            self.host, self.port, ssl=context
        )
        self.writer.write(
            (
                f"PUT {self.path} HTTP/1.1\r\nHost: {self.authority}\r\n"
                + "Transfer-Encoding: chunked\r\n\r\n"
            ).encode()
        )
        await self.writer.drain()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is None:
                await self.write_chunk(b"")
        finally:
            self.writer.close()
            await self.writer.wait_closed()

    async def write_chunk(self, data: bytes):
        size = len(data)
        data = f"{len(data):X}\r\n".encode() + data + b"\r\n"
        self.writer.write(data)
        await self.writer.drain()
        return size


async def run_upload(
    urls: list[str],
    concurrency: int,
    size_bytes: int | None,
    progress: Progress | None = None,
    task_id: TaskID | None = None,
    bytes_ref: list[int] | None = None,
    duration: float | None = None,
) -> tuple[int, float]:
    transferred = bytes_ref if bytes_ref is not None else [0]

    async def worker(index: int) -> None:
        url = urls[index % len(urls)]
        remaining = worker_size(size_bytes, concurrency, index)
        if remaining == 0:
            return
        async with PutChunkedWriter(url) as w:
            upload_chunk_size = 4 * 1024
            chunk = b"\0" * upload_chunk_size
            while remaining is None or remaining > 0:
                n = (
                    upload_chunk_size
                    if remaining is None
                    else min(upload_chunk_size, remaining)
                )
                count = await w.write_chunk(chunk[:n])
                if remaining is not None:
                    remaining -= count
                transferred[0] += count
                await asyncio.sleep(0)

    elapsed = await run_workers(
        concurrency, worker, duration, transferred, progress, task_id
    )
    return transferred[0], elapsed


def pick_strategy_urls(
    info: dict, download_size_mb: int
) -> tuple[list[str], list[str]]:
    strategy = info.get("speedStrategy")

    if isinstance(strategy, list) and strategy:
        strategy = set(strategy)
        return [
            build_download_url(base, tid=info["tid"], download_size_mb=download_size_mb)
            for base in strategy
        ], [build_upload_url(base) for base in strategy]

    raise RuntimeError("获取上传配置失败")


@app.command()
def speedtest(
    download: bool = typer.Option(True),
    upload: bool = typer.Option(True),
    download_workers: int = typer.Option(8, min=1),
    upload_workers: int = typer.Option(8, min=1),
    download_timeout: str | None = typer.Option(
        None, "--download-timeout", "--download-time", help="下载时间上限，单位 s/m/h/d"
    ),
    upload_timeout: str | None = typer.Option(
        None, "--upload-timeout", "--upload-time", help="上传时间上限，单位 s/m/h/d"
    ),
    upload_size: str | None = typer.Option(
        None, help="上传合计大小上限，单位 MB/GB/TB（M/MiB 等价）"
    ),
    download_size: str | None = typer.Option(
        None, help="下载合计大小上限，单位 MB/GB/TB（M/MiB 等价）"
    ),
):
    d_timeout = parse_duration(download_timeout, "--download-timeout")
    u_timeout = parse_duration(upload_timeout, "--upload-timeout")
    d_size = parse_size(download_size, "--download-size")
    u_size = parse_size(upload_size, "--upload-size")
    if d_timeout is None and d_size is None:
        d_timeout, d_size = 10.0, 512 * 1024**2
    if u_timeout is None and u_size is None:
        u_timeout, u_size = 10.0, 64 * 1024**2

    async def _main() -> None:
        async with httpx2.AsyncClient(
            verify=False, trust_env=False, follow_redirects=True, timeout=None
        ) as c:
            info = await fetch_userinfo(c)

        console.print()
        console.print(f"IP地址：{info['clientip']}")
        console.print(f"归属地市：{info['cityName']}")
        console.print(f"宽带帐号：{info['userAcc']}")
        console.print(f"下载带宽：{info['crmDown']}")
        console.print(f"上传带宽：{info['crmUp']}")

        console.print()

        download_urls, upload_urls = pick_strategy_urls(
            info,
            download_size_mb=100
            if d_size is None
            else max(1, min(100, math.ceil(d_size / download_workers / 1024**2))),
        )

        if download:
            download_progress = Progress(
                SpinnerColumn(),
                TextColumn("下载中"),
                BarColumn(bar_width=32),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeElapsedColumn(),
                console=console,
                speed_estimate_period=3.0,
            )
            with download_progress:
                d_task_id = download_progress.add_task("", total=d_size, completed=0)
                d_total_bytes, d_total_time = await run_download(
                    download_urls,
                    download_workers,
                    progress=download_progress,
                    task_id=d_task_id,
                    size_bytes=d_size,
                    duration=d_timeout,
                )
                d_mbps = (
                    (d_total_bytes * 8) / d_total_time / 1_000_000
                    if d_total_time > 0
                    else 0.0
                )

            line = f"[green]下载:[/green] {d_mbps:.2f} Mbps"
            console.print(line)
            console.print()

        if upload:
            upload_progress = Progress(
                SpinnerColumn(),
                TextColumn("上传中"),
                BarColumn(bar_width=32),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeElapsedColumn(),
                console=console,
                speed_estimate_period=3.0,
            )
            with upload_progress:
                u_task_id = upload_progress.add_task("", total=u_size, completed=0)
                u_total_bytes, u_total_time = await run_upload(
                    urls=upload_urls,
                    concurrency=upload_workers,
                    size_bytes=u_size,
                    progress=upload_progress,
                    task_id=u_task_id,
                    duration=u_timeout,
                )
            if u_total_time > 0:
                if u_total_bytes == 0:
                    console.print("[yellow]上传: 无有效数据[/yellow]")
                else:
                    u_mbps = (u_total_bytes * 8) / u_total_time / 1_000_000
                    line = f"[green]上传:[/green] {u_mbps:.2f} Mbps"
                    console.print(line)

    asyncio.run(_main())
