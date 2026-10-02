import re

with open('frontend/css/style.css', 'r') as f:
    css = f.read()

# Fix Autofill background
if ':-webkit-autofill' not in css:
    css += '''
/* Fix Chrome autofill background */
input:-webkit-autofill,
input:-webkit-autofill:hover, 
input:-webkit-autofill:focus, 
input:-webkit-autofill:active {
    -webkit-box-shadow: 0 0 0 30px white inset !important;
    -webkit-text-fill-color: var(--ink) !important;
    transition: background-color 5000s ease-in-out 0s;
}
'''

# Make the giant button truly giant
css = css.replace('padding: 16px 40px;', 'padding: 18px 40px; width: 340px; max-width: 100%;')
css = css.replace('font-size: 1.15rem;', 'font-size: 1.15rem; font-weight: 800;')
css = css.replace('margin-top: -15px; /* Pull up to overlap */', 'margin-top: -30px; /* Pull up to overlap */\n  position: relative;\n  z-index: 20;')

# Make inputs fit their containers better
css = css.replace('.dates-flex .input-wrap input {', '.dates-flex .input-wrap { flex: 1; min-width: 0; }\n.dates-flex .input-wrap input { width: 100%;')

with open('frontend/css/style.css', 'w') as f:
    f.write(css)
