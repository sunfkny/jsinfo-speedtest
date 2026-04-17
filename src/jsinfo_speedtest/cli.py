import asyncio
import base64
import json
import time
import urllib.parse

import niquests
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


async def fetch_userinfo(c: niquests.AsyncSession) -> dict:
    r = await c.get("http://speedauto.jsinfo.net/speedinfo/userinfo/1")
    r.raise_for_status()
    assert r.text
    text = r.text.strip()
    try:
        return json.loads(base64.b64decode(text))
    except Exception:
        return json.loads(text)


async def detect_ip_protocol(c: niquests.AsyncSession):
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


async def run_download(
    urls: list[str],
    concurrency: int,
    progress: Progress | None = None,
    task_id: TaskID | None = None,
    bytes_ref: list[int] | None = None,
) -> tuple[int, float]:

    def on_chunk(n: int) -> None:
        if bytes_ref is not None:
            bytes_ref[0] += n
            if progress is not None and task_id is not None:
                progress.update(task_id, completed=bytes_ref[0])

    async with niquests.AsyncSession() as c:
        c.verify = False
        c.trust_env = False
        start = time.perf_counter()

        async def worker(url: str) -> None:
            r = await c.get(url, stream=True)
            r.raise_for_status()
            async for chunk in await r.iter_content():
                on_chunk(len(chunk))

        tasks = [worker(urls[i % len(urls)]) for i in range(concurrency)]
        await asyncio.gather(*tasks)

        cost = time.perf_counter() - start
    return bytes_ref[0] if bytes_ref is not None else 0, cost


async def run_upload(
    urls: list[str],
    concurrency: int,
    upload_size_mb: int,
    progress: Progress | None = None,
    task_id: TaskID | None = None,
    bytes_ref: list[int] | None = None,
) -> tuple[int, float]:
    async def data_provider(total_mb: int, chunk_size: int = 16384):
        total_bytes = total_mb * 1024 * 1024
        bytes_sent = 0
        chunk = b"\0" * chunk_size
        while bytes_sent < total_bytes:
            yield chunk
            bytes_sent += chunk_size
            if bytes_ref is not None:
                bytes_ref[0] += chunk_size
                if progress is not None and task_id is not None:
                    progress.update(task_id, completed=bytes_ref[0])

    async with niquests.AsyncSession() as c:
        c.verify = False
        c.trust_env = False
        start = time.perf_counter()

        async def worker(url: str) -> None:
            r = await c.post(
                url,
                data=data_provider(upload_size_mb // concurrency),
                timeout=60,
            )
            r.raise_for_status()

        tasks = [worker(urls[i % len(urls)]) for i in range(concurrency)]
        await asyncio.gather(*tasks)

        cost = time.perf_counter() - start
    return bytes_ref[0] if bytes_ref is not None else 0, cost


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
    download_timeout: float = typer.Option(10, min=0),
    upload_timeout: float = typer.Option(10, min=0),
    upload_size: int = typer.Option(100, min=1),
    download_size: int = typer.Option(100, min=1),
):
    async def _main() -> None:
        async with niquests.AsyncSession() as c:
            c.trust_env = False
            c.verify = False
            info = await fetch_userinfo(c)

        console.print()
        console.print(f"IP地址：{info['clientip']}")
        console.print(f"归属地市：{info['cityName']}")
        console.print(f"宽带帐号：{info['userAcc']}")
        console.print(f"下载带宽：{info['crmDown']}")
        console.print(f"上传带宽：{info['crmUp']}")

        console.print()

        download_urls, upload_urls = pick_strategy_urls(
            info, download_size_mb=download_size
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
            )
            d_total_bytes, d_total_time = 0, 0.0
            d_bytes_ref = [0]
            with download_progress:
                d_task_id = download_progress.add_task("", total=None, completed=0)
                try:
                    d_total_bytes, d_total_time = await asyncio.wait_for(
                        run_download(
                            download_urls,
                            download_workers,
                            progress=download_progress,
                            task_id=d_task_id,
                            bytes_ref=d_bytes_ref,
                        ),
                        download_timeout,
                    )
                except asyncio.TimeoutError:
                    d_total_bytes = d_bytes_ref[0]
                    d_total_time = download_timeout
                d_mbps = (d_total_bytes * 8) / d_total_time / 1_000_000

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
            )
            u_total_bytes, u_total_time = 0, 0.0
            u_bytes_ref = [0]
            with upload_progress:
                u_task_id = upload_progress.add_task("", total=None, completed=0)
                try:
                    u_total_bytes, u_total_time = await asyncio.wait_for(
                        run_upload(
                            urls=upload_urls,
                            concurrency=upload_workers,
                            upload_size_mb=upload_size,
                            progress=upload_progress,
                            task_id=u_task_id,
                            bytes_ref=u_bytes_ref,
                        ),
                        upload_timeout,
                    )
                except asyncio.TimeoutError:
                    u_total_bytes = u_bytes_ref[0]
                    u_total_time = upload_timeout
                    upload_progress.update(u_task_id, completed=u_total_bytes)
            if u_total_time > 0:
                if u_total_bytes == 0:
                    console.print("[yellow]上传: 无有效数据[/yellow]")
                else:
                    u_mbps = (u_total_bytes * 8) / u_total_time / 1_000_000
                    line = f"[green]上传:[/green] {u_mbps:.2f} Mbps"
                    console.print(line)

    asyncio.run(_main())
