import csv
from dataclasses import asdict
import json
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .reader import FORMATS, extract, read_data, scan


class Viewer:
    def __init__(self, root):
        self.root = root
        self.path = None
        self.packets = []
        self.rows = []
        self.events = queue.Queue()
        self.busy = False
        root.title('CH10 Local Viewer')
        root.geometry('1200x820')
        toolbar = ttk.Frame(root, padding=8)
        toolbar.pack(fill='x')
        self.open_button = ttk.Button(toolbar, text='CH10 파일 열기', command=self.open_file)
        self.open_button.pack(side='left')
        self.export_button = ttk.Button(toolbar, text='추출 결과 CSV 저장', command=self.export)
        self.export_button.pack(side='left', padx=8)
        self.status = tk.StringVar(value='로컬 전용 · 데이터는 외부로 전송하지 않습니다.')
        ttk.Label(toolbar, textvariable=self.status).pack(side='left')
        options = ttk.LabelFrame(root, text='CH10 데이터 영역 기준 필드 정의 (채널별 데이터 헤더 포함)', padding=8)
        options.pack(fill='x', padx=8)
        self.fields = {}
        for col, (key, label, default, choices) in enumerate([
            ('channel', '채널 ID (빈칸=전체)', '', None),
            ('offset', '바이트 오프셋', '0', None),
            ('kind', '자료형', 'uint16', list(FORMATS)),
            ('endian', '바이트 순서', 'little', ['little', 'big']),
            ('scale', '배율', '1', None), ('bias', '보정값', '0', None),
        ]):
            ttk.Label(options, text=label).grid(row=0, column=col, padx=4)
            var = tk.StringVar(value=default)
            self.fields[key] = var
            widget = ttk.Combobox(options, textvariable=var, values=choices, state='readonly', width=12) if choices else ttk.Entry(options, textvariable=var, width=15)
            widget.grid(row=1, column=col, padx=4)
        self.extract_button = ttk.Button(options, text='값 추출 / 그래프', command=self.calculate)
        self.extract_button.grid(row=1, column=6, padx=8)
        ttk.Label(root, text='현재 단위: CH10 패킷당 값 1개. 내부 Ethernet 프레임 분리·UTC 변환·최신 규격 전체 해석은 미지원.').pack(anchor='w', padx=12, pady=4)
        panes = ttk.Panedwindow(root, orient='vertical')
        panes.pack(fill='both', expand=True, padx=8, pady=4)
        table_frame = ttk.Frame(panes)
        columns = ('index', 'offset', 'channel', 'type', 'length', 'data', 'rtc', 'checksum')
        self.table = ttk.Treeview(table_frame, columns=columns, show='headings', height=12)
        for key, label in zip(columns, ('패킷', '파일 위치', '채널', '종류', '패킷 길이', '데이터 길이', 'RTC (100ns tick)', '헤더 체크섬')):
            self.table.heading(key, text=label)
            self.table.column(key, width=120, stretch=True)
        bar = ttk.Scrollbar(table_frame, command=self.table.yview)
        self.table.configure(yscrollcommand=bar.set)
        bar.pack(side='right', fill='y')
        self.table.pack(fill='both', expand=True)
        self.table.bind('<<TreeviewSelect>>', self.select)
        panes.add(table_frame, weight=2)
        self.details = tk.Text(panes, height=10, wrap='none', state='disabled')
        panes.add(self.details, weight=1)
        self.canvas = tk.Canvas(panes, background='white', height=240)
        self.canvas.bind('<Configure>', lambda event: self.plot())
        panes.add(self.canvas, weight=2)
        root.after(100, self.poll)

    def launch(self, task, operation):
        if self.busy:
            return
        self.busy = True
        for button in (self.open_button, self.extract_button, self.export_button):
            button.configure(state='disabled')
        self.status.set('처리 중…')
        def worker():
            try:
                self.events.put((task, operation(), None))
            except Exception as exc:
                self.events.put((task, None, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def open_file(self):
        path = filedialog.askopenfilename(filetypes=[('CH10 files', '*.ch10 *.c10'), ('All files', '*.*')])
        if path:
            self.launch('scan', lambda: (path, scan(path)))

    def poll(self):
        try:
            task, result, error = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            for button in (self.open_button, self.extract_button, self.export_button):
                button.configure(state='normal')
            if error:
                self.status.set('실패: ' + error)
                messagebox.showerror('처리 실패', error)
            elif task == 'scan':
                self.path, self.packets = result
                self.rows = []
                self.plot()
                self.details.configure(state='normal')
                self.details.delete('1.0', 'end')
                self.details.configure(state='disabled')
                self.table.delete(*self.table.get_children())
                # Cap widget population, while retaining the full extraction index.
                for p in self.packets[:10000]:
                    self.table.insert('', 'end', iid=str(p.index), values=(p.index, f'0x{p.offset:X}', p.channel, f'0x{p.data_type:02X}', p.length, p.data_length, p.rtc, 'OK' if p.checksum_ok else 'BAD'))
                bad = sum(not p.checksum_ok for p in self.packets)
                self.status.set(f'{len(self.packets):,} 패킷 · 체크섬 경고 {bad} · 목록은 처음 10,000개 표시')
            else:
                self.rows, skipped = result
                self.status.set(f'{len(self.rows):,}개 추출 · 짧은 데이터/비유한 값 {skipped}개 제외')
                self.plot()
        self.root.after(100, self.poll)

    def select(self, event=None):
        selected = self.table.selection()
        if not selected:
            return
        packet = self.packets[int(selected[0])]
        try:
            data = read_data(self.path, packet)
        except Exception as exc:
            messagebox.showerror('읽기 실패', str(exc))
            return
        lines = [json.dumps(asdict(packet), indent=2),
                 f'\nData region at 0x{packet.data_offset:X}; preview {len(data)}/{packet.data_length} bytes']
        for offset in range(0, len(data), 16):
            chunk = data[offset:offset + 16]
            lines.append(f'{offset:08X}  {chunk.hex(" "):<47}  ' + ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk))
        self.details.configure(state='normal')
        self.details.delete('1.0', 'end')
        self.details.insert('end', '\n'.join(lines))
        self.details.configure(state='disabled')

    def calculate(self):
        if not self.path:
            return
        try:
            values = {key: var.get().strip() for key, var in self.fields.items()}
            params = dict(offset=int(values['offset'], 0), kind=values['kind'], endian=values['endian'],
                          scale=float(values['scale']), bias=float(values['bias']),
                          channel=int(values['channel'], 0) if values['channel'] else None)
        except ValueError:
            messagebox.showerror('필드 설정', '오프셋·채널은 정수(0x 표기 가능), 배율·보정값은 숫자로 입력하세요.')
            return
        self.launch('extract', lambda: extract(self.path, self.packets, **params))

    def plot(self):
        c = self.canvas
        c.delete('all')
        if not self.rows:
            c.create_text(20, 20, anchor='nw', text='필드를 설정하고 값을 추출하세요.', fill='#555')
            return
        width, height = max(c.winfo_width(), 200), max(c.winfo_height(), 120)
        xmin, xmax = self.rows[0][0], self.rows[-1][0]
        ymin, ymax = min(r[3] for r in self.rows), max(r[3] for r in self.rows)
        c.create_line(80, 20, 80, height - 40, width - 20, height - 40, fill='#888')
        c.create_text(8, 20, anchor='nw', text=f'{ymax:.5g}')
        c.create_text(8, height - 40, anchor='nw', text=f'{ymin:.5g}')
        c.create_text(80, height - 18, anchor='w', text=f'CH10 패킷 번호: {xmin} → {xmax} (시간축 아님)')
        # Draw extrema per screen column to preserve spikes in large datasets.
        buckets = {}
        amplitude = max(abs(ymin), abs(ymax), 1.)
        low, high = ymin / amplitude, ymax / amplitude
        for row in self.rows:
            x = round(80 + (row[0] - xmin) / max(xmax - xmin, 1) * (width - 100))
            y = height - 40 - (row[3] / amplitude - low) / (high - low or 1) * (height - 60)
            bucket = buckets.setdefault(x, [y, y])
            bucket[0], bucket[1] = min(bucket[0], y), max(bucket[1], y)
        points = []
        for x, (lo, hi) in buckets.items():
            c.create_line(x, lo, x, max(hi, lo + 1), fill='#1266af')
            points.extend((x, (lo + hi) / 2))
        if len(points) >= 4:
            c.create_line(*points, fill='#1266af')

    def export(self):
        if not self.rows:
            messagebox.showinfo('CSV', '먼저 값을 추출하세요.')
            return
        path = filedialog.asksaveasfilename(defaultextension='.csv', filetypes=[('CSV', '*.csv')])
        if path:
            try:
                with open(path, 'w', encoding='utf-8-sig', newline='') as stream:
                    writer = csv.writer(stream)
                    writer.writerow(('packet_index', 'channel_id', 'rtc_ticks_100ns', 'value'))
                    writer.writerows(self.rows)
            except OSError as exc:
                messagebox.showerror('CSV 저장 실패', str(exc))


def main():
    root = tk.Tk()
    Viewer(root)
    root.mainloop()
