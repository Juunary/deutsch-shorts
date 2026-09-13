# 독일어 쇼츠 (deutsch-shorts)

유튜브 쇼츠 피드를 **독일어 학습 피드**로 바꾸는 1인용 웹앱. 독일 크리에이터의 쇼츠를 공식 임베드 플레이어로 재생하고,
플레이어 **아래** 패널에 모드별 자막을 동기화해 보여준다.

- **듣기 모드**: 독일어 음성 + 한국어(또는 영어) 자막만. 독일어 원문은 탭해야 보임.
- **읽기 모드**: 독일어 자막을 크게, 모르는 단어는 루비(뜻) 표시, 문장 탭 → 번역.
- 단어 탭 → 뜻/사전 → 단어장 저장 → Anki TSV 내보내기. 번역 길게 누르기 → 고치기(골드셋·재학습 데이터).
- 쇼츠처럼 **스와이프**(위 = 다음, 아래 = 이전)로 넘김. 좋아요·난이도 버튼은 없고, 관심 주제와 얼마나 봤는지(완주/스킵)로만 순서가 바뀜.
  더빙 추정 영상엔 배지(⚙ → 오디오 트랙 안내).
- 자막 보강(번역·단어 뜻·CEFR·주제)은 **직접 학습한 소형 모델**(`ml/`)이 담당. 모델이 아직 안 본 영상은 **Google 번역**(무료 웹
  엔드포인트, 키 없음)으로 즉시 채워서 번역 자막이 항상 나온다.

계획 전문: `C:\Users\harry\.claude\plans\floofy-beaming-shell.md`

## 구성

```
app/        FastAPI 서버 (SQLite, 피드 채점, 자막 조립, 피드백, 단어장, 설정, 관리)
pipeline/   수집·자막·휴리스틱·보강·짝 매칭 CLI  (python -m pipeline <stage>)
static/     바닐라 JS PWA (빌드 없음)             ml/   자체 모델 학습 트랙 (랩 서버)
scripts/    PowerShell 운영 스크립트               spikes/  검증용 페이지·프로브
data/       channels.yaml(채널 시드), topics.yaml(주제), de_50k.txt(빈도표), app.db
```

## 설치 (집 PC, Windows)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1     # venv, 의존성, .env(APP_TOKEN 생성), DB
notepad .env                                                   # YOUTUBE_API_KEY, (선택) DEEPL_API_KEY, LLM_BACKEND
powershell -ExecutionPolicy Bypass -File scripts\run-server.ps1  # http://127.0.0.1:8000
.venv\Scripts\python -m pipeline all                           # 채널 시드 → 쇼츠 수집 → 자막 → 휴리스틱 → 보강 → 짝
.venv\Scripts\python -m pipeline stats
```

- **YouTube Data API 키**: Google Cloud 콘솔 → 프로젝트 → "YouTube Data API v3" 사용 설정 → 사용자 인증 정보 → API 키.
  키가 없으면 RSS(채널당 최신 15개)로만 수집한다.
- **번역**: 기본은 Google 번역 웹 엔드포인트(`MT_PROVIDER=google`, 키 없음). 영상당 언어별 1요청으로 묶어 보내고, 429가 나면 30분 쉰다.
  DeepL은 `MT_PROVIDER=deepl` + `DEEPL_API_KEY`일 때만 쓴다.
- **폰 접속**: `scripts\tailscale-serve.ps1` → `https://<pc>.<tailnet>.ts.net/#token=<APP_TOKEN>` (첫 접속 시 토큰 저장).
  같은 Wi-Fi에서는 `run-server.ps1 -Lan` 후 `http://<pc-ip>:8000` (PWA 설치는 HTTPS 필요).
- **무인 운영**: `scripts\install-tasks.ps1` → 서버(로그온 시), 파이프라인(매일 04:30), 랩 서버 동기화 `pull`(매시), LLM 서버(모델 있을 때).
- **iPhone**: Safari 탭으로 사용(홈 화면 추가 시 유튜브 임베드가 막히는 문제 보고됨). **Android**: 설정 → 홈 화면에 추가.

## 테스트

```powershell
.venv\Scripts\python -m pytest -q                 # 백엔드·파이프라인·ml 순수 함수
```
브라우저: `http://127.0.0.1:8000/static/tests.html` (프론트 순수 함수), `/spikes/ios_player.html` (iOS 자동재생 스파이크).

## Decisions (스파이크·조사 결과)

- **유튜브 API 약관**: 플레이어 위 오버레이 금지 → 자막·버튼은 전부 플레이어 아래. 자동재생 플레이어 1개, 백그라운드 재생 금지,
  영상·오디오 다운로드 금지 → 자막은 텍스트만 수집. 카드마다 "YouTube에서 보기" 링크.
- **Data API 검색은 하루 100회** → 검색 미사용. 채널 ID `UC…`→`UUSH…` 쇼츠 재생목록을 1 unit씩 열거. RSS(`feeds/videos.xml?playlist_id=UUSH…`)로 키 없이도 최신 15개 확인 가능.
- **자막 수집은 느리게**: `youtube-transcript-api`로 독일어 자동 자막을 받을 수 있지만, 이 회선(독일 주거용)에서 빠른 요청 약 12회 후
  `IpBlocked`(잠시 후 RSS도 404). 영상당 20–40초 간격, 시간당 8편, 차단 시 2h→4h→… 쿨다운(`settings.transcript_cooldown_until`).
- **유튜브 서버측 번역은 영어만** 제공(한국어 없음) → 한국어·영어는 Google 번역 웹 엔드포인트(`translate_a/t`, 세그먼트를 한 POST에 묶음)로
  채우고 모델 번역이 생기면 그것을 우선. 세그먼트별 개별 요청은 이 회선에서 곧 429(‘Sorry’ 페이지)가 났으므로 반드시 영상 단위로 묶고,
  429 후엔 30분 쿨다운(`settings.mt_cooldown_until`, 파이프라인과 앱 공용). 앱은 자막 요청 시 빠진 줄을 즉석에서 번역해 저장한다.
- **유튜브 페이지는 EU 동의 페이지**로 리다이렉트됨 → 핸들 해석 시 `SOCS`/`CONSENT` 쿠키 필요(`pipeline/ingest.py`).
- **Windows Smart App Control 켜짐**: 서명 안 된 컴파일 확장(`regex` 등)이 차단됨 → 순수 파이썬 패키지만 사용, 로컬 추론은
  llama.cpp 프리빌드가 막히면 **Ollama(서명됨)** 사용(`scripts\llama-server.ps1 -Ollama`).
- **지역**: 회선이 독일이므로 `REGION=DE` 로 지역 차단 필터.
- **오디오 트랙 전환은 코드로 불가** → 더빙 배지 + 안내 시트 + 짝 영상 칩("영어 버전 보기").
- 스파이크 (a)(b)(d)는 폰과 API 키가 필요해 미완: `spikes/dub_menu_notes.md` 에 결과 기록.
- **랩 서버(sailab01)**: 드라이버 550(CUDA 12.4)이라 vLLM 0.10.1·torch cu124 계열로 고정. Qwen3-32B-AWQ는 12GB×2에서 KV 캐시가 남지 않아 불가 →
  교사는 **Qwen3-14B-AWQ**(`--max-num-seqs 16`, 동시 8요청 약 466 tok/s). Qwen3 계열은 `enable_thinking=false`로 `<think>`를 꺼야 한다.
  장시간 작업은 tmux(systemd --user는 linger 불가). 자세한 환경은 서버의 `ml/SERVER_ENV.md`.

## 파이프라인 단계

`seed` 채널 시드(핸들→ID) · `ingest` 쇼츠 발견(API 또는 RSS) · `backfill` RSS 영상 상세 채우기 · `transcripts` 자막(느리게) ·
`translate` Google 번역(모델이 안 본 줄) · `heuristics` 어휘 커버리지/속도/CEFR · `enrich` 모델 보강 · `pair` 영어 짝 매칭 · `decay` 선호도 감쇠 ·
`all` 순차 실행 · `stats` 현황.

## 자체 모델

`ml/README.md` 참고. 흐름: PC에서 `ml/export_transcripts.py` → 서버로 push → 코퍼스·교사 라벨·SFT 구축 → QLoRA 학습 →
GGUF 변환 → PC로 pull → `.env` `LLM_BACKEND=local` → `pipeline enrich`.
