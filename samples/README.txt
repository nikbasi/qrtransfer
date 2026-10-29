Sample sets for the qrtransfer scanner. Each folder has:
  photos/         simulated phone photos (tilted, blurred, uneven light, noise, JPEG).
                  Upload them with "Scan photos" on the scanner page.
  pages/          the clean QR pages. Show them on a screen and photograph them yourself.
  viewer.html     slideshow for the live camera (open it, click for full screen, then
                  tap "Start camera" on the scanner and point the phone at it)
  WHAT_TO_EXPECT  file name, size and password

1_text_live_bw    note.txt    7 KB   no password          black & white, 2 photos (upload both)
2_text_photo_pw   secret.txt  10 KB  password test1234    black & white, 2 photos (upload both)
3_pdf_color_pw    sample.pdf  49 KB  password test1234    colour, 4 photos (any 3 are enough)

Expected: the progress bar fills, then the password prompt (sets 2 and 3), then the file
with "verified" under its name.
