| 해상도 | 대시보드 | tick 평균/p95/최대 (ms) | refresh | paint | Legacy JPEG | 합계 평균/p95/최대 | 33 ms 예산(p95) | 최종 추적 상태 · 광류 처리 FPS(워커) |
|---|---|---|---|---|---|---|---|---|
| 720p 1280×720 | Tracking | 1.2 / 3.6 / 4.0 | 1.7 / 2.1 / 2.4 | 3.6 / 4.1 / 5.1 | — | 6.5 / 9.4 / 10.6 | 이내 | TRACKING / flow_updated · LK 125 fps |
| 720p 1280×720 | Tracking + Legacy | 1.1 / 3.5 / 3.8 | 1.7 / 2.0 / 2.2 | 3.5 / 3.9 / 4.3 | 1.2 / 5.6 / 5.7 | 7.5 / 14.2 / 15.2 | 이내 | TRACKING / flow_updated · LK 121 fps |
| 1080p 1920×1080 | Tracking | 3.0 / 7.4 / 25.5 | 1.7 / 2.1 / 3.1 | 3.7 / 4.0 / 5.3 | — | 8.4 / 12.7 / 30.5 | 이내 | TRACKING / flow_updated · LK 200 fps |
| 1080p 1920×1080 | Tracking + Legacy | 2.8 / 6.9 / 25.2 | 1.7 / 2.1 / 3.0 | 3.7 / 4.0 / 4.5 | 1.1 / 5.2 / 5.4 | 9.3 / 17.6 / 34.5 | 이내 | TRACKING / flow_updated · LK 194 fps |
| 4K 3840×2160 | Tracking | 1.6 / 4.7 / 5.0 | 2.7 / 2.9 / 4.8 | 3.4 / 3.6 / 5.7 | — | 7.7 / 10.9 / 13.2 | 이내 | LOST / incoherent_motion · LK 10 fps |
| 4K 3840×2160 | Tracking + Legacy | 1.6 / 5.1 / 5.3 | 2.8 / 3.2 / 5.2 | 3.6 / 3.8 / 5.1 | 1.2 / 5.6 / 5.9 | 9.2 / 16.4 / 17.3 | 이내 | LOST / incoherent_motion · LK 13 fps |

측정 조건: Apple Silicon, offscreen Qt, 창 1440×900, 30 fps 합성 프레임. Tracking 대시보드 프레임 요청 활성(JPEG ≤10 Hz, 1280 폭 축소). 「Tracking + Legacy」는 Sentinel Legacy drive 대시보드도 열고 프레임 요청을 활성화해 DashboardBridge.frame(960 폭 JPEG, 150 ms 주기)을 같은 Qt 스레드에서 실행. Legacy 열의 평균은 인코딩하지 않은 프레임(0 ms)을 포함. 디코더 복사(캡처 스레드)와 실제 Retina 화면 합성 비용은 제외.
