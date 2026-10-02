import re

with open('frontend/css/style.css', 'r') as f:
    css = f.read()

css = css.replace('-webkit-box-shadow: 0 0 0 30px white inset !important;', '/* no box-shadow to preserve hover bg */')

with open('frontend/css/style.css', 'w') as f:
    f.write(css)
