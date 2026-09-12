# ml/ — 자체 모델 학습 트랙

앱의 자막 보강 과제(독일어 세그먼트 → 한국어/영어 번역 + 단어 뜻 + CEFR + 주제 + 요약, 하나의 JSON)를 수행하는
**소형 학생 모델**을 직접 학습해 집 PC에서 서비스한다. 스키마·검증기·프롬프트는 앱(`app/models.py`,
`pipeline/prompts/enrich_v1.md`)과 완전히 동일하므로, 학습 데이터의 형식이 곧 서비스 입력 형식이다.

| 장비 | 역할 |
|---|---|
| 집 PC (RTX 4070 Laptop 8GB) | 자막 수집, `transcripts.jsonl` 내보내기, 학생 모델 추론(GGUF), 앱 서버, 골드셋 검수 |
| 랩 서버 Mustree (RTX 3080 Ti 12GB × 2) | 코퍼스 정제, 교사 라벨링(vLLM), QLoRA 학습, 평가, GGUF 변환 |

## 서버 준비 (1회)

```bash
ssh Mustree
mkdir -p ~/deutsch-shorts && cd ~/deutsch-shorts
python3 -m venv ~/venvs/ds && source ~/venvs/ds/bin/activate
pip install torch --index-url https://download.pytorch.org/whl/cu124   # 드라이버에 맞는 CUDA 휠
pip install -r ml/requirements-ml.txt && python -m spacy download de_core_news_md
pip install vllm            # 교사 서빙
git clone https://github.com/ggml-org/llama.cpp ~/llama.cpp && cmake -S ~/llama.cpp -B ~/llama.cpp/build -DGGML_CUDA=ON && cmake --build ~/llama.cpp/build -j
python -c "import torch; print(torch.cuda.device_count())"   # 2 이어야 함
```

## 전체 순서

```powershell
# PC
.venv\Scripts\python ml\export_transcripts.py                  # app.db -> ml/data/work/transcripts.jsonl
powershell -File ml\sync\push_to_server.ps1                    # 코드 + transcripts.jsonl -> 서버
```
```bash
# 서버 (~/deutsch-shorts, venv 활성화)
python ml/corpora.py download && python ml/corpora.py clean && python ml/corpora.py sample
curl -L -o ml/data/raw/kaikki-de.jsonl https://kaikki.org/dictionary/German/kaikki.org-dictionary-German.jsonl
python ml/silver_gloss.py --wiktionary ml/data/raw/kaikki-de.jsonl
bash ml/serve/vllm-teacher.sh &                                # 교사 서빙 (:8000)
python ml/teacher_label.py --backend vllm --limit 3000 --concurrency 4
python ml/teacher_label.py --backend vllm --gloss-ko           # silver gloss 한국어 뜻
python ml/build_sft.py                                          # -> ml/data/sft/{train,val,test}.jsonl
python ml/bench_base.py --url http://127.0.0.1:8000 --model <candidate>   # 후보 3개 비교 후 configs/student_v1.yaml 의 base_model 확정
python ml/train.py --config ml/configs/student_v1.yaml --run student_v1   # 또는 accelerate launch --num_processes 2 ...
python ml/merge_export.py --run student_v1 --llama-cpp-dir ~/llama.cpp
bash ml/serve/llama-server.sh ml/runs/student_v1/student-q4_k_m.gguf &   # :8081
python ml/eval.py --name yt_mt --system mt
python ml/eval.py --name teacher --labels ml/data/work/teacher_labels.jsonl
python ml/eval.py --name student_v1 --url http://127.0.0.1:8081 --model student-v1
```
```powershell
# PC
powershell -File ml\sync\pull_model.ps1 -Run student_v1        # -> models\student.gguf
# .env: LLM_BACKEND=local, LOCAL_LLM_URL=http://127.0.0.1:8081 (llama-server) 또는 http://127.0.0.1:11434 (Ollama)
powershell -File scripts\llama-server.ps1                       # 또는 -Ollama
.venv\Scripts\python -m pipeline enrich --retry-failed
```

## 배포 게이트

v(n+1)은 `ml/runs/results.md` 에서 test·gold 지표(검증 통과율, chrF ko, gloss F1, CEFR 인접 정확도)가 v(n) 이상일 때만
`models\student.gguf` 를 교체한다. 교체 후 최근 영상만 `python -m pipeline enrich --force --limit 50` 로 재보강.

## 자체 개선 루프

앱의 "번역 고치기 / 뜻 틀림 / 너무 어려움·쉬움" → `corrections` 테이블 → `export_transcripts.py` 가 `gold: true` 와
`corrections` 로 내보냄 → `build_sft.py` 가 라벨을 수정본으로 덮어쓰고 골드 영상은 test 로 보냄 → 재학습.

## 데이터 카드

| 출처 | 용도 | 라이선스 / 비고 |
|---|---|---|
| 앱이 수집한 유튜브 자동 자막 (독일어) | enrich/cefr 과제 입력, 교사 라벨의 원문 | 개인 학습용, 비공개. 영상·오디오는 저장하지 않음 |
| 교사 모델 라벨 (vLLM 로컬 모델, 선택적으로 Claude) | enrich 과제 정답 | 모델 라이선스 준수; Claude 라벨은 비용 승인 후에만 |
| OPUS OpenSubtitles v2018 de-ko / de-en | 번역 과제 | 연구용. Lison & Tiedemann (2016) 인용 |
| OPUS TED2020 v1 de-ko | 번역 과제 | CC BY-NC-ND 4.0 (비상업·개인 학습에만 사용) |
| OPUS Tatoeba v2023-04-12 de-ko | 번역 과제 | CC BY 2.0 FR |
| FrequencyWords de_50k (OpenSubtitles) | 단어 레벨 휴리스틱 | CC BY-SA 4.0 (`data/de_50k.txt`) |
| kaikki.org Wiktionary 독일어 추출 | silver gloss 영어 뜻·성·복수 | CC BY-SA 4.0 |
| Flores-200 devtest, NTREX-128 | 평가 전용 | CC BY-SA 4.0 |

모델과 데이터는 개인 학습용으로만 사용하고 공개하지 않는다.

## 실험 로그

| run | base | data (enrich/translate/gloss/cefr) | steps | val loss | valid | chrF ko | gloss F1 | cefr adj | ms/video (4070) | 비고 |
|---|---|---|---|---|---|---|---|---|---|---|
| student_v1 | (TBD) | | | | | | | | | |

평가 결과 표는 `ml/runs/results.md` 에 누적된다.
