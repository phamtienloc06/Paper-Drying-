from pathlib import Path
from html import escape
from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).parent
S = 2
im = Image.new('RGB', (1600*S, 900*S), 'white')
d = ImageDraw.Draw(im)
svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="900" viewBox="0 0 1600 900"><rect width="1600" height="900" fill="white"/>']

def rect(box, fill, stroke, radius=18, width=2):
    x,y,r,b = box
    d.rounded_rectangle(tuple(int(v*S) for v in box), radius=radius*S, fill=fill, outline=stroke, width=width*S)
    svg.append(f'<rect x="{x}" y="{y}" width="{r-x}" height="{b-y}" rx="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="{width}"/>')

def text(x,y,value,size=26,color='#243449',bold=False):
    font=ImageFont.truetype('C:/Windows/Fonts/arialbd.ttf' if bold else 'C:/Windows/Fonts/arial.ttf',size*S)
    d.text((x*S,y*S),value,font=font,fill=color,anchor='mm')
    svg.append(f'<text x="{x}" y="{y}" font-family="Arial, sans-serif" font-size="{size}" font-weight="{700 if bold else 400}" fill="{color}" text-anchor="middle" dominant-baseline="central">{escape(value)}</text>')

def arrow(points, both=False, color='#52647b'):
    d.line([(x*S,y*S) for x,y in points],fill=color,width=3*S)
    svg.append('<polyline points="'+' '.join(f'{x},{y}' for x,y in points)+f'" fill="none" stroke="{color}" stroke-width="3" stroke-linejoin="round"/>')
    def head(a,b):
        import math
        dx,dy=b[0]-a[0],b[1]-a[1]
        n=math.hypot(dx,dy); ux,uy=dx/n,dy/n
        p=[b,(b[0]-12*ux+6*uy,b[1]-12*uy-6*ux),(b[0]-12*ux-6*uy,b[1]-12*uy+6*ux)]
        d.polygon([(int(x*S),int(y*S)) for x,y in p],fill=color)
        svg.append('<polygon points="'+' '.join(f'{x},{y}' for x,y in p)+f'" fill="{color}"/>')
    head(points[-2],points[-1])
    if both: head(points[1],points[0])

text(800,72,'SƠ ĐỒ HOẠT ĐỘNG HỆ THỐNG SẤY GIẤY',38,bold=True)

rect((50,170,510,725),'#f5f7fa','#94a3b8')
text(280,212,'THIẾT BỊ',28,bold=True)
rect((90,290,470,380),'#ffffff','#a4b0c0')
text(280,335,'Cảm biến nhiệt độ PT100',26,bold=True)
rect((90,450,470,545),'#ffffff','#a4b0c0')
text(280,482,'Module relay',27,bold=True)
text(280,518,'Bộ gia nhiệt • Quạt',25)
rect((625,170,1025,725),'#edf4ff','#5087d5')
text(825,212,'MÁY 1',30,'#24559b',True)
text(825,251,'Điều khiển cục bộ',25)
rect((660,290,990,545),'#ffffff','#85a7d5')
text(825,347,'Bộ điều khiển',30,bold=True)
text(825,401,'AUTO / MANUAL',26)
text(825,451,'Xử lý nhiệt độ và lệnh',24)
text(825,496,'Kiểm tra lỗi, bảo vệ',24)
rect((660,605,990,675),'#ffffff','#85a7d5')
text(825,640,'HMI • Cài đặt, vận hành',25,bold=True)
arrow([(825,605),(825,545)],both=True)
arrow([(470,335),(660,335)])
text(560,308,'Nhiệt độ',23)
arrow([(660,495),(470,495)])
text(560,468,'Bật / tắt',23)
text(565,405,'Modbus',22)
text(565,434,'RS485',22)

rect((1150,170,1550,410),'#fff6e9','#d5a04e')
text(1350,212,'ĐÁM MÂY',28,'#91631d',True)
text(1350,277,'ThingsBoard',32,bold=True)
text(1350,328,'Dữ liệu • Lịch sử',25)
text(1350,369,'Cảnh báo',25)
arrow([(990,335),(1090,335),(1090,290),(1150,290)],both=True)
text(1081,253,'MQTT',23)

rect((1150,490,1550,725),'#eff9f2','#67a780')
text(1350,532,'MÁY 2',30,'#2e7148',True)
text(1350,589,'SCADA từ xa',30,bold=True)
text(1350,639,'Giám sát và gửi lệnh',25)
text(1350,680,'điều khiển',25)
arrow([(1350,410),(1350,490)],both=True)
text(1458,450,'Internet',22)

svg.append('</svg>')
(OUT/'so_do_hoat_dong.svg').write_text('\n'.join(svg),encoding='utf-8')
im.save(OUT/'so_do_hoat_dong.png',dpi=(192,192))
print(OUT/'so_do_hoat_dong.png')
