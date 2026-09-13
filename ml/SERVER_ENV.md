# SERVER_ENV.md — 랩 서버 ML 환경 (deutsch-shorts)

작성일 2026-09-13 · 호스트 `sailab01` (서버 측 Claude Code가 작성, 개인 식별 정보는 제거한 판)

이 머신이 곧 GPU 서버다. 게이트웨이가 아니고 내부에 별도 GPU 호스트는 없다 (`nvidia-smi`가 로컬에서 GPU 2장을 직접 보고함).

---

## 1. SSH 키 (집 PC → 서버)

원인은 집 PC의 공개키가 `~/.ssh/authorized_keys`에 **아예 없었던 것**. 권한(`~` 750, `~/.ssh` 700, `authorized_keys` 600)과
sshd 설정(OpenSSH 8.9 기본값, `PubkeyAuthentication yes`, 사용자 제한 없음)은 처음부터 정상이었다.

조치: 기존 줄의 CRLF(`\r\n`)를 LF로 정규화(`sed -i 's/\r$//'`) → PC 키를 정확히 한 줄로 추가 →
`chmod go-w ~ ; chmod 700 ~/.ssh ; chmod 600 ~/.ssh/authorized_keys`. sshd 재시작은 불필요(`authorized_keys`는 접속 때마다 읽힘).

검증: 서버에서 `ssh-keygen -lf ~/.ssh/authorized_keys`에 PC 키 지문이 보이고, PC에서
`ssh -o PreferredAuthentications=publickey <alias>`가 암호 없이 붙으면 끝. 실패하면 PC에서 `ssh -vvv`,
서버에서 `grep "<집 PC IP>" /var/log/auth.log | tail`로 사유 확인(계정이 `adm` 그룹이라 sudo 없이 읽힘).
Windows에서 키를 붙여넣으면 `\r`이 다시 섞이니, 추가 후 지문 확인을 습관화할 것.

---

## 2. 하드웨어 · 툴체인

| 항목 | 값 |
|---|---|
| OS | Ubuntu 22.04.4 LTS (jammy) |
| 커널 | 6.8.0-138-generic |
| CPU | AMD Ryzen 9 5950X 16C/32T (`nproc` 32) |
| RAM | 125 GiB (available 116) |
| 디스크 | `/dev/nvme0n1p2` ext4 916G, **여유 221G** (요구 150G 충족) |
| GPU | RTX 3080 Ti 12038 MiB × 2 (idle, 타 사용자 프로세스 없음) |
| 드라이버 | 550.144.03 — **CUDA 12.4** |
| nvcc | 12.1 (system) |
| sudo | 그룹에는 속함(`sudo`, `adm`, `docker`)이나 **암호 필요** → 이번 작업에서 sudo 미사용 |

도구: `uv 0.5.26`, `conda 24.5.0`, `cmake 3.22.1`, `gcc/g++ 11.4.0`, `git 2.34.1`,
`tmux 3.2a`, `screen 4.09`, `gh 2.88.1`, `curl`, `wget`, `make`. `ninja` 없음(불필요).
시스템 `python3.10.12`, `python3.11`은 없어서 **uv가 CPython 3.11.11을 받아 venv 생성**.

네트워크(전부 도달 가능): pypi.org 200 · huggingface.co 200 · github.com 200 ·
object.pouta.csc.fi 200 · kaikki.org 200 · download.pytorch.org/whl/cu124 200
(루트 `/`만 403인데 이는 정상 — 인덱스 경로는 200).

포트: **8000, 8081 모두 비어 있음**. 단 공용 서버라 8001/8080/8090/443/80/5432/27017 등은 타 서비스가 점유 중.
vLLM은 8000, llama-server는 8081 그대로 쓰면 된다.

---

## 3. 설치 결과

### 코드

`~/deutsch-shorts` — 공개 저장소라 인증 불필요. HEAD `14abfce`. **아무것도 커밋/수정하지 않았다**
(`git status` clean, 이 파일만 새로 추가). `ml/data/{raw,work,sft,gold,feedback}`, `ml/runs` 생성 완료.

### `~/venvs/ds` — 학습용 (Python 3.11.11)

| 패키지 | 버전 |
|---|---|
| torch | **2.6.0+cu124** |
| transformers | 5.17.0 |
| peft | 0.20.0 |
| trl | 1.13.0 |
| bitsandbytes | 0.50.2 |
| accelerate | 1.15.0 |
| datasets | 5.0.1 |
| spacy | 3.8.16 + `de_core_news_md` 3.8.0 |
| sacrebleu | 2.6.0 · gguf 0.19.0 · numpy 2.4.6 |

검증 결과:

- `torch.cuda.device_count()` → **2**, 둘 다 `NVIDIA GeForce RTX 3080 Ti` 12038 MiB.
- `python -m pytest tests/test_ml.py -q` → **4 passed**.
  (conftest가 `app.config`를 임포트해서 루트 `requirements.txt`와 `pytest`도 같은 venv에 설치해야 했다.)
- `ml/build_sft.py` · `ml/eval.py` · `ml/train.py` · `ml/corpora.py` · `ml/teacher_label.py` ·
  `ml/merge_export.py` 전부 `--help` 정상.
- `ml/common.py` 임포트 정상 (`app.models`, 프롬프트 연결 확인).
- **bitsandbytes 4bit**: `Qwen/Qwen2.5-0.5B-Instruct`를 `load_in_4bit`(nf4 + double quant, bf16 compute)로
  GPU0에 로드 → `Linear4bit` 확인, 444 MiB 점유, 독일어→한국어 한 문장 생성 성공 (34.6 tok/s).
- **TRL `assistant_only_loss`: 있음** (trl 1.13.0). `ml/train.py`의 1차 경로(`max_length` + `assistant_only_loss`)가
  그대로 동작하므로 `max_seq_length` 폴백은 타지 않는다. 참고로 trl 1.13에는 `max_seq_length`가 **제거**되어
  폴백 경로는 이제 죽은 코드다.

주의 — transformers 5.x API 변경: `tokenizer.apply_chat_template(..., return_tensors="pt")`가 텐서가 아니라
`BatchEncoding`을 돌려준다. `return_dict=True`로 받아 `model.generate(**enc)`로 넘겨야 한다.
직접 추론 스크립트를 쓸 때 걸린다(학습 경로는 무관).

### `~/venvs/vllm` — 교사 서빙용 (Python 3.11.11)

| 패키지 | 버전 |
|---|---|
| vllm | **0.10.1.1** |
| torch | 2.7.1+cu126 |
| transformers | 4.57.6 (핀) |
| xformers 0.0.31 · xgrammar 0.1.21 |

**왜 최신 vLLM이 아닌가 (중요):**

`pip install vllm`은 **vllm 0.29.0 → torch 2.13.0+cu130**을 끌고 오는데, CUDA 13 휠은 **드라이버 580 이상**을 요구한다.
이 서버 드라이버는 550.144.03이라 `torch.cuda.is_available()`이 `False`가 되고
`The NVIDIA driver on your system is too old (found version 12040)`로 죽는다.
공용 서버라 드라이버 업그레이드는 하지 않았다. 대신 CUDA 12.x 계열인 **vllm 0.10.1.1(torch 2.7.1+cu126)**로 내렸고,
CUDA 12의 minor version 호환성 덕에 드라이버 550에서 정상 동작한다(matmul 검증 완료).

transformers는 5.17.0이 함께 잡혔으나 vLLM 0.10은 4.x 기준이라 `transformers<5`로 핀했다.

### `~/llama.cpp`

- commit `acecd56`, `cmake -DGGML_CUDA=ON` 빌드 성공 (`-DLLAMA_CURL=OFF`, OpenSSL 없어 HTTPS 다운로드만 비활성 — 변환·양자화에는 무관).
- `build/bin/llama-quantize --help` 정상.
- `build/bin/llama-cli --list-devices` → `CUDA0`, `CUDA1` 둘 다 인식.
- (ds venv) `python ~/llama.cpp/convert_hf_to_gguf.py --help` 정상.
- NCCL 미검출 경고가 있으나 단일 GPU 추론에는 영향 없음.

---

## 4. 교사 모델 (vLLM) — **Qwen3-32B-AWQ는 이 장비에서 불가**

### Qwen3-32B-AWQ (19 GB, 다운로드는 완료, 캐시에 있음) — 탈락

두 가지를 다 만족시킬 수 없다:

1. 기본 설정(`vllm-teacher.sh` 그대로, TP=2, util 0.92) →
   가중치가 GPU당 9.06 GiB를 먹고 **KV 캐시가 0.23 GiB밖에 안 남는다**.
   `ValueError: ... estimated maximum model length is 1840` 로 기동 실패.
   8192 컨텍스트에 1.00 GiB가 필요한데 턱없이 부족.
2. `--kv-cache-dtype fp8 --gpu-memory-utilization 0.97`로 우회하면 **기동은 된다**.
   그러나 로그에 `--kv-cache-dtype is not supported by the V1 Engine. Falling back to V0.` —
   **V0 엔진에서는 `response_format: json_schema`가 무시된다.** 실제로 스키마를 준 요청이
   JSON이 아닌 평문(`Der Satz bedeutet: ...`)으로 돌아왔고 `<think>` 블록까지 그대로 나왔다.

교사 라벨링 파이프라인은 **JSON 스키마 강제 디코딩이 전제**(`teacher_label.py`가 앱과 동일한 스키마·검증기를 씀)이므로
V0 폴백은 받아들일 수 없다. 게다가 KV 0.5 GiB로는 동시성도 사실상 1이라 수천 개 라벨링에 비현실적이다.

### 대체: **Qwen3-14B-AWQ** (9.4 GB) — 채택

```bash
vllm serve Qwen/Qwen3-14B-AWQ --quantization awq_marlin --tensor-parallel-size 2 \
  --max-model-len 8192 --gpu-memory-utilization 0.92 --max-num-seqs 16 \
  --port 8000 --served-model-name Qwen/Qwen3-14B-AWQ
```

- **V1 엔진**으로 기동, 약 60초. KV 캐시 **4.60 GiB/GPU = 76,528 토큰** (8192 컨텍스트 9개 동시분).
- `--max-num-seqs 16`이 필요하다. 기본값(256)이면 샘플러 워밍업에서
  `CUDA out of memory occurred when warming up sampler with 256 dummy requests`로 죽는다.
  `teacher.yaml`의 `concurrency: 4`에는 16이면 충분하고도 남는다.
- **JSON 스키마 강제 디코딩 동작 확인** — 지정된 `response_format {"type":"json_schema", ...}` 요청이
  스키마에 맞는 JSON으로만 반환됨(`json.loads` 통과).

처리 속도 (5필드 enrich 유사 과제, `max_tokens=400`, `enable_thinking=false`):

| 동시성 | 생성 tok/s | 전체(in+out) tok/s | 요청당 | 평균 지연 |
|---|---|---|---|---|
| 1 | 90 | 159 | 1.16 s | 1.2 s |
| 4 | 210 | 375 | 0.49 s | 1.8 s |
| 8 | 466 | 835 | 0.22 s | 1.6 s |

한국어 출력 품질도 양호했다. 예:
`{"ko":"오늘은 독일에서 은행 계좌를 개설하는 방법을 보여줄게요.","cefr":"A1",...}`

동시성 8까지 여유가 있으니 `ml/configs/teacher.yaml`의 `concurrency`는 4 → 8로 올려도 된다.

**테스트 후 서버는 종료했고 GPU는 반납된 상태다** (`nvidia-smi` compute apps 없음, 25/11 MiB만 사용).

> `ml/configs/teacher.yaml`의 `vllm.model`은 아직 `Qwen/Qwen3-32B-AWQ`다. 저장소를 건드리지 않는다는 규칙에 따라
> 수정하지 않았으니, 라벨링 시작 전에 `Qwen/Qwen3-14B-AWQ`로 바꾸고 `vllm-teacher.sh`에 `--max-num-seqs 16`을 추가해야 한다.

---

## 5. 학생 후보 모델

| 모델 | 상태 | 크기 |
|---|---|---|
| `Qwen/Qwen3-4B` | **다운로드 완료** | 7.6 GB |
| `google/gemma-3-4b-it` | **건너뜀** — 게이트 모델. `Access denied. This repository requires approval.` | — |

gemma를 쓰려면 HF 계정으로 모델 페이지에서 라이선스에 동의한 뒤 `HF_TOKEN`을 설정해야 한다.
`ml/bench_base.py`로 후보를 비교하려면 Qwen3-4B 하나로는 부족하니, 게이트 없는 3~4B 다국어 instruct 모델
(예: `mistralai/Ministral-3b-instruct`, `microsoft/Phi-4-mini-instruct`)을 하나 더 받는 것을 권한다.

---

## 6. 공개 데이터

`ml/corpora.py`의 SOURCES URL 4개 전부 **유효(HTTP 200)** — 수정 불필요.

| 파일 | 줄 수 | 크기 |
|---|---|---|
| `ml/data/raw/opensubtitles_de_ko.zip` | — | 26 MB |
| `ml/data/raw/opensubtitles_de_en.zip` | — | 678 MB |
| `ml/data/raw/ted2020_de_ko.zip` | — | 23 MB |
| `ml/data/raw/tatoeba_de_ko.zip` | — | 53 KB |
| `ml/data/raw/kaikki-de.jsonl` | **371,261** | 1.1 GB |
| `ml/data/work/pairs_de_ko.jsonl` | **689,110** | 117 MB |
| `ml/data/work/pairs_de_en.jsonl` | **15,316,908** | 2.0 GB |
| `ml/data/work/translate_de_ko.jsonl` | **150,000** | 25 MB |
| `ml/data/work/translate_de_en.jsonl` | **50,000** | 7.0 MB |

소요: download 2분 54초 · clean 2분 31초 · sample 59초 · kaikki 3분 11초.

아직 안 한 것: `ml/corpora.py eval-sets` (Flores-200), `ml/silver_gloss.py --wiktionary ml/data/raw/kaikki-de.jsonl`.
둘 다 요청 범위 밖이라 남겨뒀다.

---

## 7. 장시간 학습 실행 방법

**tmux를 쓸 것.** `systemd --user`는 돌아가지만 `Linger=no`라서 **마지막 SSH 세션이 끊기면 유저 서비스도 죽는다**
(`sudo loginctl enable-linger <user>`가 필요한데 sudo 암호가 필요해 설정하지 않았다).

```bash
# 1) tmux — 권장. 끊겨도 살아남고, 다시 붙어서 진행 상황을 볼 수 있다.
tmux new -s train
source ~/venvs/ds/bin/activate && cd ~/deutsch-shorts
accelerate launch --num_processes 2 ml/train.py --config ml/configs/student_v1.yaml --run student_v1
# Ctrl-b d 로 분리, 나중에:  tmux attach -t train
```

```bash
# 2) nohup — 로그만 남기면 충분할 때
cd ~/deutsch-shorts && source ~/venvs/ds/bin/activate
nohup accelerate launch --num_processes 2 ml/train.py \
  --config ml/configs/student_v1.yaml --run student_v1 \
  > ~/deutsch-shorts/ml/runs/student_v1.log 2>&1 &
tail -f ~/deutsch-shorts/ml/runs/student_v1.log
```

```bash
# 3) 교사 서빙도 같은 방식 (라벨링 끝나면 반드시 내려서 GPU 반납)
tmux new -s teacher
source ~/venvs/vllm/bin/activate && cd ~/deutsch-shorts
vllm serve Qwen/Qwen3-14B-AWQ --quantization awq_marlin --tensor-parallel-size 2 \
  --max-model-len 8192 --gpu-memory-utilization 0.92 --max-num-seqs 16 \
  --port 8000 --served-model-name Qwen/Qwen3-14B-AWQ
```

**공용 서버 에티켓**: GPU 작업 전 `nvidia-smi`로 남의 프로세스를 확인하고, 서빙이 끝나면 반드시 종료할 것.
`--tensor-parallel-size 2`는 GPU 2장을 전부 잡으므로 학습과 동시에 돌릴 수 없다.

---

## 8. 경로 · 디스크

| 항목 | 경로 | 크기 |
|---|---|---|
| 코드 | `~/deutsch-shorts` | 약 40 MB |
| 학습 venv | `~/venvs/ds` | 5.7 GB |
| 서빙 venv | `~/venvs/vllm` | 7.9 GB |
| llama.cpp | `~/llama.cpp` (+`build/`) | 2.0 GB |
| HF 캐시 | `~/.cache/huggingface` | 49 GB |
| 데이터 | `~/deutsch-shorts/ml/data` | 3.9 GB |

디스크: 916G 중 **여유 221G**. 학습 산출물(merged fp16 4B ≈ 8 GB, f16 GGUF ≈ 8 GB, Q4 GGUF ≈ 2.5 GB,
체크포인트 수 GB)을 감안해도 충분하다. 공용 서버이므로 여유는 계속 주시할 것.

---

## 9. 미해결 문제와 제안

1. **`ml/configs/teacher.yaml`의 교사 모델을 `Qwen/Qwen3-14B-AWQ`로 바꿔야 한다.**
   `ml/serve/vllm-teacher.sh`에도 `--max-num-seqs 16`이 필요하다(없으면 샘플러 워밍업 OOM).
   저장소 무수정 규칙 때문에 손대지 않았다. `concurrency`도 4 → 8 권장.
2. **Qwen3-32B-AWQ 19 GB가 HF 캐시에 남아 있다.** 이 장비에서 쓸 수 없으므로
   디스크가 아쉬우면 `hf cache delete` 또는
   `rm -rf ~/.cache/huggingface/hub/models--Qwen--Qwen3-32B-AWQ`로 정리해도 된다.
   (다만 나중에 드라이버를 올리거나 GPU를 늘리면 재검토 가치는 있다.)
3. **드라이버 550이 상한이다.** 최신 vLLM(0.29+, CUDA 13)을 쓰려면 580+로 올려야 하는데,
   공용 서버라 다른 사용자 영향이 있으니 합의 후 진행할 일이다. 지금 구성으로도 교사 라벨링에는 문제없다.
4. **Qwen3는 기본적으로 `<think>` 블록을 낸다.** 라벨링 요청에는
   `chat_template_kwargs: {"enable_thinking": false}`를 넣어야 토큰 낭비와 파싱 오류를 막을 수 있다.
   `ml/teacher_label.py`가 이걸 보내는지 확인 필요.
5. **transformers 5.17 / trl 1.13이 저장소가 상정한 버전(4.45 / 0.20)보다 훨씬 앞선다.**
   테스트는 통과했고 `assistant_only_loss` 경로도 살아 있지만, 실제 QLoRA 학습은 아직 안 돌려봤다.
   첫 학습은 작은 스텝으로 스모크 테스트한 뒤 본 학습에 들어갈 것.
6. **학생 후보가 Qwen3-4B 하나뿐**이다(gemma는 게이트). `ml/bench_base.py` 비교를 제대로 하려면
   게이트 없는 3~4B 다국어 instruct 모델을 하나 더 받거나, gemma 라이선스에 동의하고 `HF_TOKEN`을 설정할 것.
7. **`sudo`는 암호가 필요해 쓰지 않았다.** `loginctl enable-linger <user>`(systemd --user 상주),
   드라이버 업그레이드, 시스템 패키지 설치가 필요하면 암호가 있어야 한다. 현재까진 전부 불필요했다.
8. **`authorized_keys`의 CRLF 재발 주의.** Windows에서 키를 붙여넣으면 또 `\r`이 섞인다.
   추가 후 `ssh-keygen -lf ~/.ssh/authorized_keys`로 지문이 보이는지 항상 확인할 것.
