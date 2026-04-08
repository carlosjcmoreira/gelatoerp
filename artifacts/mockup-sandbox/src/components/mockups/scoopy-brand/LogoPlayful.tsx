import React from "react";
import { Card } from "@/components/ui/card";

export function LogoPlayful() {
  return (
    <div className="w-full h-full min-h-screen bg-[#FFF8F0] flex items-center justify-center p-8 font-['Nunito'] text-[#1E1E1E]">
      <div className="max-w-4xl w-full grid gap-12">
        <div className="text-center space-y-4">
          <h1 className="text-4xl font-extrabold text-[#E8614A] tracking-tight">Scoopy</h1>
          <p className="text-sm text-[#F5A623] font-bold uppercase tracking-widest">Brand Identity • Playful</p>
        </div>

        <div className="grid md:grid-cols-2 gap-8">
          {/* Horizontal Lockup */}
          <Card className="p-12 flex flex-col items-center justify-center bg-white shadow-md border-0 rounded-[2rem] h-[300px] gap-8 transition-transform hover:-translate-y-2 duration-300">
            <div className="flex items-center gap-4">
              <svg width="56" height="56" viewBox="0 0 56 56" fill="none" xmlns="http://www.w3.org/2000/svg" className="drop-shadow-sm">
                <circle cx="28" cy="22" r="16" fill="#F5A623"/>
                <path d="M12 22C12 22 18 30 28 30C38 30 44 22 44 22C44 22 40 38 28 38C16 38 12 22 12 22Z" fill="#E8614A"/>
                <path d="M20 38L28 52L36 38H20Z" fill="#2BB5A0"/>
              </svg>
              <span className="text-5xl font-black tracking-tight text-[#E8614A]">Scoopy</span>
            </div>
            <span className="text-xs text-[#2BB5A0] font-bold tracking-widest uppercase bg-[#2BB5A0]/10 px-4 py-2 rounded-full">Horizontal Lockup</span>
          </Card>

          {/* Stacked Lockup */}
          <Card className="p-12 flex flex-col items-center justify-center bg-white shadow-md border-0 rounded-[2rem] h-[300px] gap-8 transition-transform hover:-translate-y-2 duration-300">
            <div className="flex flex-col items-center gap-2">
              <svg width="80" height="80" viewBox="0 0 56 56" fill="none" xmlns="http://www.w3.org/2000/svg" className="drop-shadow-sm">
                <circle cx="28" cy="22" r="16" fill="#F5A623"/>
                <path d="M12 22C12 22 18 30 28 30C38 30 44 22 44 22C44 22 40 38 28 38C16 38 12 22 12 22Z" fill="#E8614A"/>
                <path d="M20 38L28 52L36 38H20Z" fill="#2BB5A0"/>
              </svg>
              <span className="text-4xl font-black tracking-tight text-[#E8614A]">Scoopy</span>
            </div>
            <span className="text-xs text-[#F5A623] font-bold tracking-widest uppercase bg-[#F5A623]/10 px-4 py-2 rounded-full">Stacked Lockup</span>
          </Card>
        </div>

        {/* Dark/Accent Variant */}
        <div className="mt-8">
          <Card className="p-12 flex flex-col items-center justify-center bg-[#2BB5A0] shadow-xl border-0 rounded-[2rem] h-[300px] gap-8">
            <div className="flex items-center gap-6">
               <svg width="64" height="64" viewBox="0 0 56 56" fill="none" xmlns="http://www.w3.org/2000/svg" className="drop-shadow-md">
                <circle cx="28" cy="22" r="16" fill="#FFF8F0"/>
                <path d="M12 22C12 22 18 30 28 30C38 30 44 22 44 22C44 22 40 38 28 38C16 38 12 22 12 22Z" fill="#F5A623"/>
                <path d="M20 38L28 52L36 38H20Z" fill="#E8614A"/>
              </svg>
              <span className="text-6xl font-black tracking-tight text-[#FFF8F0] drop-shadow-sm">Scoopy</span>
            </div>
            <span className="text-xs text-[#FFF8F0] font-bold tracking-widest uppercase bg-black/10 px-4 py-2 rounded-full">Accent Mode Application</span>
          </Card>
        </div>
      </div>
    </div>
  );
}
