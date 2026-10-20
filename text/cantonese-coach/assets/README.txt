這個目錄放「學員自己的音檔」，不入庫。

建議放法：

    assets/
    ├── input/day01-dialogue.mp3      # 教材或港劇截取的音檔（30–60 秒）
    ├── record/day01-self.m4a         # 學員自己的錄音（同一段內容）
    └── notes/error-log.md            # 錯題本（只寫錯因，不寫正確答案）

為甚麼要有 record/：
發音訓練唯一有效的驗收方式是「錄音 → 對比原音 → 只改一個問題」。
沒有自己的錄音，聲調永遠停在「聽起來差不多」。

命名規則：dayNN-內容.m4a，方便與 out/plan/schedule.csv 的日期對上。

不要把 mp3 提交進 git（版權 + 體積）；這個目錄除本文件外全部被 .gitignore 忽略。
