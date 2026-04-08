import React from "react";
import { Card } from "@/components/ui/card";

export function LogoModern() {
  return (
    <div className="w-full h-full min-h-screen bg-[#FFF8F2] flex items-center justify-center p-8 font-['DM_Sans'] text-[#1E1E1E]">
      <div className="max-w-4xl w-full grid gap-12">
        <div className="text-center space-y-4">
          <h1 className="text-3xl tracking-tight text-[#1A7A6E] font-medium">Scoopy</h1>
          <p className="text-sm text-[#1E1E1E]/60 uppercase tracking-widest">Brand Identity • Modern</p>
        </div>

        <div className="grid md:grid-cols-2 gap-8">
          {/* Horizontal Lockup */}
          <Card className="p-12 flex flex-col items-center justify-center bg-white shadow-sm border-0 rounded-2xl h-[300px] gap-8 transition-transform hover:scale-[1.02] duration-300">
            <div className="flex items-center gap-4">
              <svg width="48" height="48" viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path d="M24 6C16.268 6 10 12.268 10 20C10 24.1687 11.8217 27.913 14.708 30.5L24 42L33.292 30.5C36.1783 27.913 38 24.1687 38 20C38 12.268 31.732 6 24 6Z" fill="#1A7A6E"/>
                <path d="M24 12C19.5817 12 16 15.5817 16 20C16 22.3846 17.0427 24.524 18.707 25.9868L24 32.5L29.293 25.9868C30.9573 24.524 32 22.3846 32 20C32 15.5817 28.4183 12 24 12Z" fill="#FFF8F2"/>
              </svg>
              <span className="text-5xl font-semibold tracking-tighter text-[#1E1E1E]">Scoopy</span>
            </div>
            <span className="text-xs text-[#1A7A6E] font-medium tracking-widest uppercase">Horizontal Lockup</span>
          </Card>

          {/* Stacked Lockup */}
          <Card className="p-12 flex flex-col items-center justify-center bg-white shadow-sm border-0 rounded-2xl h-[300px] gap-8 transition-transform hover:scale-[1.02] duration-300">
            <div className="flex flex-col items-center gap-4">
              <svg width="64" height="64" viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path d="M24 6C16.268 6 10 12.268 10 20C10 24.1687 11.8217 27.913 14.708 30.5L24 42L33.292 30.5C36.1783 27.913 38 24.1687 38 20C38 12.268 31.732 6 24 6Z" fill="#1A7A6E"/>
                <path d="M24 12C19.5817 12 16 15.5817 16 20C16 22.3846 17.0427 24.524 18.707 25.9868L24 32.5L29.293 25.9868C30.9573 24.524 32 22.3846 32 20C32 15.5817 28.4183 12 24 12Z" fill="#FFF8F2"/>
              </svg>
              <span className="text-4xl font-semibold tracking-tighter text-[#1E1E1E]">Scoopy</span>
            </div>
            <span className="text-xs text-[#1A7A6E] font-medium tracking-widest uppercase">Stacked Lockup</span>
          </Card>
        </div>

        {/* Dark Mode Variant */}
        <div className="mt-8">
          <Card className="p-12 flex flex-col items-center justify-center bg-[#1E1E1E] shadow-xl border-0 rounded-2xl h-[300px] gap-8">
            <div className="flex items-center gap-6">
               <svg width="56" height="56" viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path d="M24 6C16.268 6 10 12.268 10 20C10 24.1687 11.8217 27.913 14.708 30.5L24 42L33.292 30.5C36.1783 27.913 38 24.1687 38 20C38 12.268 31.732 6 24 6Z" fill="#FFF8F2"/>
                <path d="M24 12C19.5817 12 16 15.5817 16 20C16 22.3846 17.0427 24.524 18.707 25.9868L24 32.5L29.293 25.9868C30.9573 24.524 32 22.3846 32 20C32 15.5817 28.4183 12 24 12Z" fill="#1A7A6E"/>
              </svg>
              <span className="text-6xl font-semibold tracking-tighter text-[#FFF8F2]">Scoopy</span>
            </div>
            <span className="text-xs text-[#FFF8F2]/60 font-medium tracking-widest uppercase">Dark Mode Application</span>
          </Card>
        </div>
      </div>
    </div>
  );
}
