# PYSAR's "custom" window chrome

Short story behind this:

Roughly 3 years ago, I wanted to create a UI application using a custom title bar and window chrome (like i.e. IntelliJ,
VSCode, Spotify, etc.) using Java as a base language. The problem was, I was not happy with what was around at the time.
I wanted easy-to-use, lightweight and responsive customizable windows.

So I embarked on the journey and made a private library for Windows 10/11 and MacOS ARM.
Long story short, I wanted the same for Pysar. BUT since Pysar is not in Java (or C#) but uses the most
scuffed setup ever, I decided to before I end up using Electron, I rather hook into pywebview
and customize the window chrome myself.

The code for that is coming from my private library (I used JNA and later FFM to make that work in Java, 
here I have access to ctypes.) Most of it can be surely found on the internet. 
This might be a dumb choice, but it was fun doing that nontheless!

So I tried to make it modular enough so it works on Linux, MacOS and Windows (Windows causing the most pain until now).
But unnecessary/not needed code is omitted. I literally just took what I needed to make my UI work.

So that's it. Enjoy!

~Ogu