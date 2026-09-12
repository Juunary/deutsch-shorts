# 스파이크 기록

## (a) iOS Safari: 첫 탭 후 단일 플레이어 + loadVideoById 소리 자동재생
- 페이지: `https://<pc>.<tailnet>.ts.net/spikes/ios_player.html` (서버 실행 후)
- 순서: 1. Start 탭 → A 재생(소리) 확인 → 2. 타이머 버튼(제스처 없이 2초 후 B 로드) → PLAYING 상태·소리 확인 → 3. 스와이프 영역 터치 → C
- 결과: (iPhone) ___ / (Android Chrome) ___
- 데스크톱 Chromium(앱 내 브라우저)에서는 탭 → 재생 → 자막 동기화까지 정상 (2026-09-12)

## (b) 임베드 플레이어 ⚙ 메뉴의 "오디오 트랙"
- 자동 더빙된 쇼츠를 앱 카드에서 재생하고 플레이어 ⚙(설정)에 "오디오 트랙"이 있는지 확인
- 결과: (iPhone) ___ / (Android) ___
- 없으면: 배지 + "YouTube에서 보기" 딥링크 + 안내 문구로 축소 (이미 그렇게 동작)

## (c) 집 IP에서 자막 수집 (2026-09-12, 독일 주거용 회선)
- 채널 RSS(`feeds/videos.xml?channel_id=` / `playlist_id=UUSH…`) 동작 확인 → 15개 최신 쇼츠
- `youtube-transcript-api`: 독일어 자동 자막 fetch 성공 (30 snippet). 유튜브 서버측 번역은 **영어만** 제공(한국어 불가)
- 약 12회 요청 후 `IpBlocked` → 약 1시간 뒤에는 RSS까지 404. 결론: 영상당 20–40초 간격, 시간당 소량(8편), 차단 시 2h·4h… 쿨다운
- 번역 폴백: DeepL(키) 또는 gtx(무키, 429 잦음); 근본 해결은 로컬 모델(Ollama 스톡 모델 → 학생 모델)

## (d) Data API 키 + UUSH
- 키 발급 전. 키가 생기면 `spikes\uush_probe.py @EasyGerman` 실행 후 여기 기록
