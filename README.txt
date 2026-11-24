SlideCapture Pro Android v1.6

PC용 SlideCapture Pro v1.6을 안드로이드 앱으로 옮긴 버전입니다.
PC 변환 엔진(slide_rebuild.py, ppt_worker.py)을 그대로 앱 안에서 실행(Chaquopy)하므로
변환 결과(도형·선·아이콘 프리폼, 표, 검증 OCR 텍스트, 원본 동일 모드)가 PC와 같은 수준입니다.

[기능]
1. 입력
   - 폰 카메라 촬영: 화면·프로젝터를 찍으면 화면 테두리를 자동으로 찾아 사다리꼴을 반듯하게 펴고 슬라이드만 자름
     (화면만 꽉 차게 찍었거나 테두리가 안 보이면 보정하지 않음)
   - USB HDMI 캡처보드(OTG): 기기가 외장 카메라(UVC)로 인식하면 ⚙ 환경설정에서 입력 소스로 선택
     좌우 검은 여백(필러박스)은 화면 경계에서 정확히 잘라냄 (PC v1.5와 같은 방식)
   - 자동 캡처: 화면이 바뀌고 잠시 멈추면 자동 캡처 (민감도: ⚙ 환경설정)
2. 캡처된 슬라이드: 미리보기 · 저장 폴더 열기 · 기존파일열기
   - 썸네일을 누르면 선택, 한 번 더 누르면 미리보기
   - 기존파일열기: 갤러리·파일에서 PNG/JPG 여러 장 선택해 목록 뒤에 이어 붙임
     이미 있는 파일·열 수 없는 파일은 건너뛰고 몇 장 불러왔는지 안내
3. 미리보기: 이전 · 다음 · 삭제(빨간색, 확인 후 삭제하고 다음 슬라이드 표시) · 파일열기 · 닫기, 좌우로 밀어서 이동
4. PPT 변환 옵션 9개 (길게 누르면 한 줄 도움말)
   화면 비율 자동조절 / 화면 자동 보정 / OCR 텍스트 객체 / 실제 PPT 표 복원 / 사각형·선 벡터 /
   원본 배경 유지 / 텍스트 배경 복원 / 네이티브 객체 우선 / 원본 동일(투명 텍스트)
5. PPT 변환은 백그라운드(별도 프로세스의 포그라운드 서비스)에서 실행
   - 변환 중에도 캡처·미리보기·삭제·불러오기 가능, 앱을 내려도 알림창에 진행률 표시하며 계속 변환
   - 버튼 'PPT 변환 중 n/N · 취소' 또는 알림의 '취소'로 취소
   - 결과: 다운로드/SlideCapture Pro/SlideCapture_Pro_v1.6.pptx (+ 분석 보고서 JSON), 완료 후 열기·공유
6. 이름·버전: 헤더 'SlideCapture Pro v1.6'. 버전은 app/build.gradle.kts 의 appVersion 한 곳에서 관리
   (헤더·PPT 기본 파일명·APK 이름이 자동으로 따라 바뀜)

[참고]
 - 안드로이드 10 이상, 64비트(arm64) 기기
 - 한글 OCR: Tesseract4Android + tessdata_fast(kor, eng) - PC에서 쓰는 언어 데이터와 같은 파일
 - 글자 검증용 글꼴: 나눔고딕(OFL, assets/fonts/OFL.txt). PPT 글꼴 지정은 PC와 같이 '맑은 고딕'
 - 폰은 PC보다 느려서 슬라이드 1장 변환에 수십 초가 걸릴 수 있음
 - USB 캡처보드는 기기가 외장 카메라로 인식해야 사용 가능(주로 안드로이드 14 이상·일부 기기)

[빌드]
 - GitHub: 저장소에 올리면 Actions(.github/workflows/build.yml)가 APK를 만들어 'apk' 아티팩트로 제공
 - Android Studio: 이 폴더를 열고 Build > Build APK(s). 빌드 PC에 Python 3.8 필요(Chaquopy)

[구성]
 app/src/main/java/com/slidecapture/pro/  MainActivity(메인), PreviewActivity(미리보기), ConvertService(변환), SlideStore, App
 app/src/main/python/                     slide_rebuild.py·ppt_worker.py(PC v1.6 엔진 그대로),
                                          phone_capture.py(카메라 화면 보정·검정 여백), mobile_worker.py(변환 진입점),
                                          pytesseract/(Tesseract4Android 연결 - PC pytesseract와 같은 결과 형식)
