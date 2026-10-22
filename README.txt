관심종목 모멘텀 추적기
====================

실행: 바탕화면의 "관심종목 모멘텀" 아이콘을 더블클릭하면 최신 종가로 계산한 뒤 HTML 이 브라우저에서 열립니다.
(같은 내용이 이 폴더의 "관심종목_모멘텀_최신.html" 과 out\watchlist_날짜.html 에 저장됩니다.)

관심종목 바꾸기: config.json 의 "watchlist" 에서 티커·이름을 추가/삭제 (미국 종목만).
  예) {"ticker": "MSFT", "name": "마이크로소프트"},  — 마지막 항목 뒤에는 쉼표를 붙이지 않습니다.

필요한 것: Python 3 + yfinance, pandas  (pip install -r requirements.txt)
직접 실행: python momentum.py [--no-open] [--asof YYYY-MM-DD]

계산 정의는 "업종 섹터 모멘텀 추적기"와 같습니다 (배당 미반영 종가, 1M·3M 수익률, 3주 궤적, 사분면·▲▼ 신호, 속도 변화).
상장 3개월 미만 종목은 표에만 나오고 차트·신호에서는 빠집니다.
투자 참고용이며 투자 권유가 아닙니다.

스마트폰 앱 (PWA)
-----------------
index.html·data.json·manifest.webmanifest·sw.js·icon-*.png 가 스마트폰 앱입니다. GitHub Pages 로 올리면 안드로이드 크롬에서
"홈 화면에 추가/앱 설치"로 아이콘을 만들 수 있고, GitHub Actions(.github\workflows\update.yml)가
평일 한국시간 07:30 에 data.json 을 자동 갱신합니다. PC 에서 실행.bat 을 돌려도 data.json 이 같이 갱신됩니다.
