import json
from pathlib import Path
from huggingface_hub import hf_hub_download

root = Path.cwd()
download_dir = root / "data"
selected_file = (
    root
    / "Weak_Scenario_Mining/outputs/experiment_001"
    / "selected_additional_files.txt"
)
manifest_file = (
    root
    / "Weak_Scenario_Mining/data"
    / "bench2drive_full+sup_13638.json"
)

repos = [
    "rethinklab/Bench2Drive-Full",
    "rethinklab/Bench2Drive-Full-Sup",
]

selected = [
    line.strip()
    for line in selected_file.read_text(encoding="utf-8").splitlines()
    if line.strip()
]
manifest = json.loads(manifest_file.read_text(encoding="utf-8"))

download_dir.mkdir(parents=True, exist_ok=True)

for index, filename in enumerate(selected, 1):
    destination = download_dir / filename
    expected_size = int(manifest[filename]["size"])

    if destination.is_file():
        actual_size = destination.stat().st_size

        if actual_size == expected_size:
            print(f"[skip {index}/{len(selected)}] {filename}")
            continue

        # 크기가 잘못된 파일은 삭제하지 않고 백업
        backup = destination.with_name(
            f"{destination.name}.bad-size-{actual_size}"
        )
        suffix = 1
        while backup.exists():
            backup = destination.with_name(
                f"{destination.name}.bad-size-{actual_size}-{suffix}"
            )
            suffix += 1

        destination.rename(backup)
        print(
            f"[invalid] {filename}: "
            f"{actual_size} != {expected_size}, backup={backup.name}"
        )

    errors = []

    for repo_id in repos:
        try:
            print(
                f"[download {index}/{len(selected)}] "
                f"{repo_id} / {filename}"
            )
            hf_hub_download(
                repo_id=repo_id,
                repo_type="dataset",
                filename=filename,
                local_dir=str(download_dir),
            )

            if not destination.is_file():
                raise RuntimeError("다운로드 후 파일이 생성되지 않았습니다.")

            actual_size = destination.stat().st_size
            if actual_size != expected_size:
                raise RuntimeError(
                    f"파일 크기 불일치: {actual_size} != {expected_size}"
                )

            print(f"[ok] {filename} ({actual_size:,} bytes)")
            break

        except Exception as exc:
            errors.append(f"{repo_id}: {exc}")
    else:
        raise RuntimeError(
            f"\n다운로드 실패: {filename}\n" + "\n".join(errors)
        )

print(f"\n완료: 선택된 {len(selected)}개 파일의 용량이 모두 정상입니다.")