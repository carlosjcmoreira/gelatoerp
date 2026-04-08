import React from 'react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';

const colors = [
  { name: 'Primary', hex: '#1A7A6E', role: 'Deep Teal' },
  { name: 'Secondary / Accent', hex: '#E8614A', role: 'Warm Coral' },
  { name: 'Warm Accent', hex: '#F5A623', role: 'Sunny Yellow' },
  { name: 'Mint / Fresh', hex: '#2BB5A0', role: 'Mint' },
  { name: 'Background Light', hex: '#FFF8F2', role: 'Cream' },
  { name: 'Background Dark', hex: '#1E2B2A', role: 'Dark Slate' },
  { name: 'Text Dark', hex: '#1E1E1E', role: 'Almost Black' },
  { name: 'Success', hex: '#3DAA68', role: 'Green' },
  { name: 'Danger', hex: '#D93025', role: 'Red' },
];

export function ColorPalette() {
  // Inject Nunito font just for this component showcase
  React.useEffect(() => {
    const link = document.createElement('link');
    link.href = 'https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800&display=swap';
    link.rel = 'stylesheet';
    document.head.appendChild(link);
    return () => {
      document.head.removeChild(link);
    };
  }, []);

  return (
    <div className="min-h-screen bg-slate-50 p-8 flex items-center justify-center font-sans">
      <div className="w-full max-w-[1080px] bg-white rounded-3xl shadow-xl overflow-hidden border border-slate-200">
        
        {/* Header */}
        <div className="bg-[#1E2B2A] text-[#FFF8F2] p-10 flex items-center justify-between">
          <div>
            <h1 className="text-4xl font-extrabold mb-2" style={{ fontFamily: 'Nunito, sans-serif' }}>Scoopy</h1>
            <p className="text-[#2BB5A0] font-medium tracking-wide uppercase text-sm">Brand Design System</p>
          </div>
          <div className="text-right">
            <p className="text-sm opacity-70">Porto, Portugal</p>
            <p className="text-sm opacity-70">Artisan Gelato & Pastelaria</p>
          </div>
        </div>

        <div className="p-10 space-y-16">
          
          {/* Colors Grid */}
          <section>
            <h2 className="text-sm uppercase tracking-widest text-slate-400 font-bold mb-6">Color Palette</h2>
            <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-6">
              {colors.map((c) => (
                <div key={c.hex} className="group">
                  <div 
                    className="w-full aspect-square rounded-2xl shadow-sm border border-slate-100 mb-4 transition-transform group-hover:scale-105 group-hover:shadow-md"
                    style={{ backgroundColor: c.hex }}
                  />
                  <div>
                    <h3 className="font-bold text-[#1E1E1E] text-sm">{c.name}</h3>
                    <p className="text-slate-500 text-xs font-mono mt-1 uppercase">{c.hex}</p>
                    <p className="text-slate-400 text-xs mt-0.5">{c.role}</p>
                  </div>
                </div>
              ))}
            </div>
          </section>

          <hr className="border-slate-100" />

          {/* Typography */}
          <section>
            <h2 className="text-sm uppercase tracking-widest text-slate-400 font-bold mb-6">Typography Specimen</h2>
            <div className="grid md:grid-cols-2 gap-12">
              <div className="bg-[#FFF8F2] p-8 rounded-2xl border border-[#E8614A]/10">
                <div className="mb-4">
                  <span className="text-xs font-bold text-[#E8614A] uppercase tracking-wider">Heading Font</span>
                  <p className="text-[#1E1E1E] font-medium mt-1">Nunito (Google Fonts)</p>
                </div>
                <div className="space-y-4 text-[#1E2B2A]" style={{ fontFamily: 'Nunito, sans-serif' }}>
                  <h1 className="text-5xl font-extrabold leading-tight">Artisan Gelato.</h1>
                  <h2 className="text-3xl font-bold">Made with joy in Porto.</h2>
                  <h3 className="text-xl font-semibold">Taste the Mediterranean sunshine.</h3>
                </div>
              </div>
              
              <div className="bg-slate-50 p-8 rounded-2xl border border-slate-100">
                <div className="mb-4">
                  <span className="text-xs font-bold text-slate-500 uppercase tracking-wider">Body Font</span>
                  <p className="text-[#1E1E1E] font-medium mt-1">System Sans (Inter/San Francisco)</p>
                </div>
                <div className="space-y-4 text-[#1E1E1E] font-sans">
                  <p className="text-base leading-relaxed">
                    Our gelato is crafted daily using traditional methods and the freshest local ingredients. 
                    Every scoop is a celebration of flavor, designed to bring a smile to your face.
                  </p>
                  <p className="text-sm leading-relaxed text-slate-600">
                    Whether you're strolling down the Ribeira or taking a break from work, Scoopy offers a moment of pure, unadulterated joy in every bite.
                  </p>
                </div>
              </div>
            </div>
          </section>

          <hr className="border-slate-100" />

          {/* Brand in Use */}
          <section>
            <h2 className="text-sm uppercase tracking-widest text-slate-400 font-bold mb-6">Brand in Use</h2>
            <div className="bg-[#FFF8F2] p-8 rounded-2xl border border-[#1A7A6E]/10 flex flex-col md:flex-row items-center justify-between gap-8">
              
              <div className="flex items-center gap-4">
                <div className="w-12 h-12 rounded-xl bg-[#E8614A] flex items-center justify-center shadow-lg shadow-[#E8614A]/20">
                  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <circle cx="12" cy="8" r="5" fill="#FFF8F2" />
                    <path d="M8 12L12 21L16 12H8Z" fill="#F5A623" />
                  </svg>
                </div>
                <h1 className="text-3xl font-extrabold text-[#1A7A6E]" style={{ fontFamily: 'Nunito, sans-serif' }}>Scoopy</h1>
              </div>

              <div className="flex items-center gap-6">
                <Badge style={{ backgroundColor: '#2BB5A0', color: '#FFF8F2' }} className="px-3 py-1 text-sm font-semibold shadow-sm hover:bg-[#1A7A6E]">
                  New Flavor
                </Badge>
                
                <Button style={{ backgroundColor: '#E8614A', color: '#FFF8F2' }} className="font-bold px-6 py-6 rounded-full shadow-lg shadow-[#E8614A]/30 hover:bg-[#D93025] hover:scale-105 transition-all">
                  Order Now
                </Button>
              </div>

            </div>
          </section>

        </div>
      </div>
    </div>
  );
}

export default ColorPalette;
