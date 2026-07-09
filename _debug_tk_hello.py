import tkinter as tk
print("Starting Tkinter test window...")
root = tk.Tk()
root.title("Test Window")
tk.Label(root, text="If you can see this, Tkinter works fine from a file.").pack(padx=20, pady=20)
root.mainloop()
print("Window closed normally.")