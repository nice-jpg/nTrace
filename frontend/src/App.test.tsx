import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { UserInputsSection } from './App'

describe('trace input details', () => {
  it('labels and formats message, reasoning, and function call inputs', () => {
    render(<UserInputsSection inputs={[
      { type: 'message', role: 'human', content: 'hello\nworld' },
      { type: 'reasoning', summary: [{ type: 'summary_text', text: 'Inspect the page' }] },
      { type: 'function_call', name: 'search', arguments: { query: 'shop' } },
    ]} />)

    expect(screen.getByText('message · human')).toBeInTheDocument()
    expect(screen.getByText(/hello\s+world/)).toBeInTheDocument()
    expect(screen.getByText('reasoning')).toBeInTheDocument()
    expect(screen.getByText('Inspect the page')).toBeInTheDocument()
    expect(screen.getByText('function_call')).toBeInTheDocument()
    expect(screen.getByText('search')).toBeInTheDocument()
    expect(screen.getByText(/"query": "shop"/)).toBeInTheDocument()
  })
})
